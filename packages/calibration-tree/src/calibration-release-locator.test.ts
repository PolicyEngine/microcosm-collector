import { afterEach, expect, test } from "bun:test";

import {
  calibrationReleaseFromManifest,
  requestedReleaseId,
  resolveHfBuildFolderSha,
  resolveHfCommitPublishedAt,
  resolveHfRepositoryCommitPublishedAt,
  resolveHfReleaseDirectorySha,
  resolveHfRevisionSha,
} from "./calibration-release-locator";
import { withCalibrationTreeManifestEntry, emptyCalibrationTreeManifest } from "./calibration-tree-manifest";

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

test("latest release identity is read only from the dashboard manifest", () => {
  const manifest = withCalibrationTreeManifestEntry(
    emptyCalibrationTreeManifest(),
    "us",
    {
      buildArtifactId: "a".repeat(64),
      kind: "release",
      sourceId: "microcosm-us-new-release",
      label: "microcosm-us-new-release",
      releaseId: "microcosm-us-new-release",
      stagingRunId: null,
      hfRepo: "policyengine/populace-us",
      hfCommitSha: "1234567890abcdef1234567890abcdef12345678",
      treeSchemaVersion: 6,
      indexSha256: "c".repeat(64),
      indexBytes: 20,
      createdAt: "2026-09-15T12:00:00.000Z",
      updatedAt: "2026-09-15T12:00:00.000Z",
    },
    true,
  );
  expect(calibrationReleaseFromManifest(manifest, "us")).toEqual({
    buildArtifactId: "a".repeat(64),
    releaseId: "microcosm-us-new-release",
    releaseDirectory: "microcosm-us-new-release",
    hfCommitSha: "1234567890abcdef1234567890abcdef12345678",
    updatedAt: "2026-09-15T12:00:00.000Z",
  });
});

test("a release cut is located by its tag and read from its line folder", () => {
  const cut = "microcosm-zz-2024-national-20261019T083323Z-0a1b2c3d";
  const manifest = withCalibrationTreeManifestEntry(
    emptyCalibrationTreeManifest(),
    "uk",
    {
      buildArtifactId: "b".repeat(64),
      kind: "release",
      sourceId: cut,
      label: cut,
      releaseId: cut,
      releaseDirectory: "microcosm-zz-2024-national",
      stagingRunId: null,
      hfRepo: "policyengine/populace-uk-private",
      hfCommitSha: "2".repeat(40),
      treeSchemaVersion: 6,
      indexSha256: "c".repeat(64),
      indexBytes: 20,
      createdAt: "2026-10-19T10:21:22.000Z",
      updatedAt: "2026-10-19T10:21:22.000Z",
    },
    true,
  );
  expect(calibrationReleaseFromManifest(manifest, "uk", cut)).toEqual({
    buildArtifactId: "b".repeat(64),
    releaseId: cut,
    releaseDirectory: "microcosm-zz-2024-national",
    hfCommitSha: "2".repeat(40),
    updatedAt: "2026-10-19T10:21:22.000Z",
  });
  expect(calibrationReleaseFromManifest(manifest, "uk").releaseId).toBe(cut);
});

test("a build in its own folder is located and read by that folder", () => {
  const build = "microcosm-zz-2024-national/zz-calibration-attempt-20261019T083323Z-0a1b2c3d";
  const manifest = withCalibrationTreeManifestEntry(
    emptyCalibrationTreeManifest(),
    "uk",
    {
      buildArtifactId: "b".repeat(64),
      kind: "release",
      sourceId: build,
      label: build,
      releaseId: build,
      stagingRunId: null,
      hfRepo: "policyengine/populace-uk-private",
      hfCommitSha: "2".repeat(40),
      treeSchemaVersion: 6,
      indexSha256: "c".repeat(64),
      indexBytes: 20,
      createdAt: "2026-10-19T08:33:23.000Z",
      updatedAt: "2026-10-19T08:33:23.000Z",
    },
    true,
  );
  expect(calibrationReleaseFromManifest(manifest, "uk", build)).toMatchObject({
    releaseId: build,
    releaseDirectory: build,
    hfCommitSha: "2".repeat(40),
  });
});

test("a requested release may name a build folder, never a path that leaves releases/", () => {
  expect(requestedReleaseId("")).toBe("latest");
  expect(requestedReleaseId("latest")).toBe("latest");
  expect(requestedReleaseId("microcosm-uk-2024-25-national")).toBe("microcosm-uk-2024-25-national");
  expect(requestedReleaseId("microcosm-uk-2024-25-national/uk-frs-calibration-attempt-x"))
    .toBe("microcosm-uk-2024-25-national/uk-frs-calibration-attempt-x");
  for (const value of ["../latest", "a/..", "a/b/c", "a//b", "/a", "a b"]) {
    expect(() => requestedReleaseId(value), value).toThrow("Invalid release id.");
  }
});

test("a build folder's commit is the newest commit behind its files, read at the given revision", async () => {
  const revision = "5".repeat(40);
  const requested: string[] = [];
  globalThis.fetch = (async (input: string | URL | Request) => {
    requested.push(String(input));
    return Response.json([
      { type: "file", path: "releases/r/b/calibration_diagnostics.json", lastCommit: { id: "1".repeat(40), date: "2026-10-19T08:00:00Z" } },
      { type: "file", path: "releases/r/b/microcosm_uk_2024_25.h5", lastCommit: { id: "2".repeat(40), date: "2026-10-19T09:00:00Z" } },
    ]);
  }) as typeof fetch;
  await expect(resolveHfBuildFolderSha("uk", "r/b", revision)).resolves.toBe("2".repeat(40));
  expect(requested).toEqual([
    `https://huggingface.co/api/datasets/policyengine/populace-uk-private/tree/${revision}/releases/r/b?recursive=false&expand=true`,
  ]);
  await expect(resolveHfBuildFolderSha("uk", "r", revision)).rejects.toThrow("Invalid build folder.");
});

test("historical release tags resolve to exact Hugging Face commits", async () => {
  globalThis.fetch = (async (input: string | URL | Request) => {
    expect(String(input)).toContain("/revision/microcosm-us-history");
    return Response.json({ sha: "ABCDEF1234567890ABCDEF1234567890ABCDEF12" });
  }) as typeof fetch;
  expect(await resolveHfRevisionSha("us", "microcosm-us-history", 0)).toBe(
    "abcdef1234567890abcdef1234567890abcdef12",
  );
});

test("untagged release directories resolve to their newest source commit", async () => {
  const diagnosticsCommit = "1".repeat(40);
  const manifestCommit = "2".repeat(40);
  globalThis.fetch = (async (input: string | URL | Request) => {
    expect(String(input)).toBe(
      "https://huggingface.co/api/datasets/policyengine/populace-be-private/" +
        "tree/main/releases/microcosm-be-history?recursive=false&expand=true",
    );
    return Response.json([
      {
        type: "file",
        path: "releases/microcosm-be-history/calibration_diagnostics.json",
        lastCommit: {
          id: diagnosticsCommit,
          date: "2026-08-23T16:13:38.000Z",
        },
      },
      {
        type: "file",
        path: "releases/microcosm-be-history/release_manifest.json",
        lastCommit: {
          id: manifestCommit.toUpperCase(),
          date: "2026-08-23T16:14:38.000Z",
        },
      },
    ]);
  }) as typeof fetch;
  expect(await resolveHfReleaseDirectorySha(
    "be",
    "microcosm-be-history",
  )).toBe(manifestCommit);
});

test("untagged release resolution requires calibration diagnostics", async () => {
  globalThis.fetch = (async (_input: string | URL | Request) => Response.json([
    {
      type: "file",
      path: "releases/microcosm-be-history/release_manifest.json",
      lastCommit: {
        id: "3".repeat(40),
        date: "2026-08-23T16:14:38.000Z",
      },
    },
  ])) as typeof fetch;
  await expect(resolveHfReleaseDirectorySha(
    "be",
    "microcosm-be-history",
  )).rejects.toThrow("has no calibration diagnostics");
});

test("a build's publication timestamp is its pinned commit's date", async () => {
  // microcosm-uk-2024-25-national carries no timestamp in its id, so the
  // one source is the commit log at the exact revision the build pins.
  const sha = "a".repeat(40);
  const requested: string[] = [];
  globalThis.fetch = (async (input: string | URL | Request) => {
    requested.push(String(input));
    return Response.json([
      { id: sha.toUpperCase(), date: "2026-10-04T18:57:22.000Z", title: "Publish" },
      { id: "b".repeat(40), date: "2026-10-02T23:15:56.000Z", title: "Stage" },
    ]);
  }) as typeof fetch;
  expect(await resolveHfCommitPublishedAt("uk", sha)).toBe("2026-10-04T18:57:22.000Z");
  expect(requested).toEqual([
    `https://huggingface.co/api/datasets/policyengine/populace-uk-private/commits/${sha}?limit=1`,
  ]);
});

test("a commit log that does not start at the pinned commit is refused", async () => {
  globalThis.fetch = (async (_input: string | URL | Request) =>
    Response.json([{ id: "c".repeat(40), date: "2026-10-04T18:57:22.000Z" }])
  ) as typeof fetch;
  await expect(resolveHfCommitPublishedAt("uk", "a".repeat(40))).rejects.toThrow(
    "invalid commit metadata",
  );
  globalThis.fetch = (async (_input: string | URL | Request) =>
    new Response(null, { status: 404 })
  ) as typeof fetch;
  await expect(resolveHfCommitPublishedAt("uk", "a".repeat(40))).rejects.toThrow(
    "was not found",
  );
});

test("a recorded repository's commit is read from that repository, not the country's", async () => {
  const sha = "a".repeat(40);
  const requested: string[] = [];
  globalThis.fetch = (async (input: string | URL | Request) => {
    requested.push(String(input));
    return Response.json([{ id: sha, date: "2026-10-04T18:57:22.000Z" }]);
  }) as typeof fetch;
  expect(
    await resolveHfRepositoryCommitPublishedAt("policyengine/populace-uk-archive", sha),
  ).toBe("2026-10-04T18:57:22.000Z");
  expect(requested).toEqual([
    `https://huggingface.co/api/datasets/policyengine/populace-uk-archive/commits/${sha}?limit=1`,
  ]);
  await expect(
    resolveHfRepositoryCommitPublishedAt("not a repository id", sha),
  ).rejects.toThrow("owner/name");
});
