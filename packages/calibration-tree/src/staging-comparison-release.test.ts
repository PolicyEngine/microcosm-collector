import { expect, test } from "bun:test";

import {
  emptyCalibrationTreeManifest,
  withCalibrationTreeManifestEntry,
  type CalibrationTreeManifest,
  type CalibrationTreeManifestEntry,
} from "./calibration-tree-manifest";
import { stagingLocalAreaPipelines } from "./countries";
import { InvalidReleaseIdError, type ReleaseEntry } from "./latest-artifact";
import {
  loadStagingTargetChangeDataset,
  newestPublishedLocalAreaRelease,
  resolveStagingComparisonRelease,
  stagingBaselineReleases,
  stagingRunPipelineId,
  stagingResolveUrl,
  type StagingComparisonReleaseDependencies,
} from "./staging-artifact";
import { missingReleasesRecord } from "./test-support/releases-record-responses";

// UK shapes as published on 19 Oct 2026. A local-area candidate's staged
// receipt and run manifest record no dataset role, and its dataset file name
// changed between builds (microcosm_uk_2025_local.h5 in September,
// microcosm_uk_2024_25_local.h5 in October). The run manifest's pipeline id,
// which the staging contract validates, is stable: uk-local-candidate for
// local-area candidates and uk-frs-calibration for national attempts.
const NATIONAL_FOLDER = "microcosm-uk-2024-25-national";
const DENSE_FOLDER = "microcosm-uk-2024-25-dense";
const NATIONAL_CURRENT = `${NATIONAL_FOLDER}-20261019T083323Z-6cba5d71`;
const NATIONAL_EARLIER = `${NATIONAL_FOLDER}-20261014T214443Z-d12ff9b3`;
const DENSE_EARLIER = `${DENSE_FOLDER}-20261005T101500Z-0d0d0d0d`;
const DENSE_NEWEST = `${DENSE_FOLDER}-20261019T000432Z-ee5c2dbe`;
const JUNE_RELEASE = "populace-uk-2023-dd68c73-4aa4b14-20260619T023711Z";
const LOCAL_CANDIDATE_RUN = "uk-local-candidate-f100-s42-20261019T000432Z-ee5c2dbe";
const NATIONAL_ATTEMPT_RUN = "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71";

function releaseBuild(
  releaseId: string,
  createdAt: string,
  buildDigit: string,
  releaseDirectory?: string,
): CalibrationTreeManifestEntry {
  return {
    buildArtifactId: buildDigit.repeat(64),
    kind: "release",
    sourceId: releaseId,
    label: releaseId,
    releaseId,
    ...(releaseDirectory ? { releaseDirectory } : {}),
    stagingRunId: null,
    hfRepo: "policyengine/populace-uk-private",
    hfCommitSha: buildDigit.repeat(40),
    treeSchemaVersion: 6,
    indexSha256: "e".repeat(64),
    indexBytes: 10,
    createdAt,
    updatedAt: createdAt,
  };
}

function ukManifest(): CalibrationTreeManifest {
  const stagingBuild: CalibrationTreeManifestEntry = {
    ...releaseBuild(LOCAL_CANDIDATE_RUN, "2026-10-19T02:05:10.000Z", "9"),
    kind: "staging",
    releaseId: null,
    stagingRunId: LOCAL_CANDIDATE_RUN,
    hfRepo: "policyengine/populace-uk-staging",
  };
  return [
    releaseBuild(JUNE_RELEASE, "2026-06-19T02:38:38.000Z", "1"),
    releaseBuild(NATIONAL_EARLIER, "2026-10-14T22:09:45.000Z", "2", NATIONAL_FOLDER),
    releaseBuild(NATIONAL_CURRENT, "2026-10-19T10:21:22.000Z", "3", NATIONAL_FOLDER),
    releaseBuild(DENSE_EARLIER, "2026-10-05T10:20:00.000Z", "4", DENSE_FOLDER),
    releaseBuild(DENSE_NEWEST, "2026-10-19T07:53:18.000Z", "5", DENSE_FOLDER),
    stagingBuild,
  ].reduce(
    (manifest, entry) => withCalibrationTreeManifestEntry(manifest, "uk", entry),
    emptyCalibrationTreeManifest(),
  );
}

function inventoryEntry(releaseId: string, isLocalArea: boolean): ReleaseEntry {
  return {
    release_id: releaseId,
    date: releaseId,
    files: [],
    has_calibration: true,
    dataset_role: isLocalArea ? "non_default_local_area" : null,
    is_default: !isLocalArea,
    is_local_area: isLocalArea,
  };
}

// Roles as the release manifests record them (dataset_role), read the way the
// release picker reads them.
const UK_INVENTORY = [
  inventoryEntry(JUNE_RELEASE, false),
  inventoryEntry(NATIONAL_EARLIER, false),
  inventoryEntry(NATIONAL_CURRENT, false),
  inventoryEntry(DENSE_EARLIER, true),
  inventoryEntry(DENSE_NEWEST, true),
];

test("only the UK registration names a staging pipeline that builds local-area candidates", () => {
  expect(stagingLocalAreaPipelines("uk")).toEqual(["uk-local-candidate"]);
  expect(stagingLocalAreaPipelines("us")).toEqual([]);
  expect(stagingLocalAreaPipelines("be")).toEqual([]);
});

test("the newest local-area release build is the latest one the publisher's manifest records", () => {
  expect(newestPublishedLocalAreaRelease(ukManifest(), "uk", UK_INVENTORY)?.releaseId)
    .toBe(DENSE_NEWEST);
  // Without a local-area role in the inventory, no release qualifies.
  expect(newestPublishedLocalAreaRelease(
    ukManifest(),
    "uk",
    UK_INVENTORY.map((entry) => ({ ...entry, is_local_area: false })),
  )).toBeNull();
});

function dependencies(
  overrides: Partial<StagingComparisonReleaseDependencies>,
): StagingComparisonReleaseDependencies {
  return {
    runPipelineId: async () => {
      throw new Error("The run's pipeline must not be read.");
    },
    currentReleaseId: async () => {
      throw new Error("The current release must not be read.");
    },
    newestLocalAreaReleaseId: async () => {
      throw new Error("The local-area release must not be read.");
    },
    ...overrides,
  };
}

test("US and Belgian staging candidates keep comparing with the current release", async () => {
  for (const [country, current] of [
    ["us", "populace-us-2024-spm-receipts-20260923"],
    ["be", "microcosm-be-2026-chronicle-3cef97b-20260823T134247Z"],
  ] as const) {
    await expect(resolveStagingComparisonRelease(
      "any-run",
      country,
      dependencies({ currentReleaseId: async () => current }),
    )).resolves.toEqual({
      release_id: current,
      basis: "current_release",
      local_area_candidate: false,
    });
  }
});

test("a UK national candidate compares with the current national build", async () => {
  await expect(resolveStagingComparisonRelease(
    NATIONAL_ATTEMPT_RUN,
    "uk",
    dependencies({
      runPipelineId: async () => "uk-frs-calibration",
      currentReleaseId: async () => NATIONAL_CURRENT,
    }),
  )).resolves.toEqual({
    release_id: NATIONAL_CURRENT,
    basis: "current_release",
    local_area_candidate: false,
  });
});

test("a UK local-area candidate compares with the newest published local-area build", async () => {
  await expect(resolveStagingComparisonRelease(
    LOCAL_CANDIDATE_RUN,
    "uk",
    dependencies({
      runPipelineId: async () => "uk-local-candidate",
      newestLocalAreaReleaseId: async () => DENSE_NEWEST,
    }),
  )).resolves.toEqual({
    release_id: DENSE_NEWEST,
    basis: "newest_local_area_release",
    local_area_candidate: true,
  });
});

test("a UK local-area candidate with no published local-area release falls back to the current release", async () => {
  await expect(resolveStagingComparisonRelease(
    LOCAL_CANDIDATE_RUN,
    "uk",
    dependencies({
      runPipelineId: async () => "uk-local-candidate",
      newestLocalAreaReleaseId: async () => null,
      currentReleaseId: async () => NATIONAL_CURRENT,
    }),
  )).resolves.toEqual({
    release_id: NATIONAL_CURRENT,
    basis: "current_release",
    local_area_candidate: true,
  });
});

test("a staging run's pipeline id is read from its run manifest", async () => {
  const originalFetch = globalThis.fetch;
  const manifestUrl = stagingResolveUrl(`runs/${LOCAL_CANDIDATE_RUN}/run_manifest.json`, "uk");
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    if (url === manifestUrl) {
      return Response.json({
        schema_version: 3,
        run_id: LOCAL_CANDIDATE_RUN,
        pipeline: { id: "uk-local-candidate", version: "0.1.0" },
      });
    }
    return new Response(null, { status: 404 });
  }) as typeof fetch;
  try {
    await expect(stagingRunPipelineId(LOCAL_CANDIDATE_RUN, 0, "uk")).resolves.toBe("uk-local-candidate");
    await expect(stagingRunPipelineId("missing-run", 0, "uk")).resolves.toBeNull();
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// A repository with releases.json (microcosm-emitter#24): the local-area
// release is the one releases.json gives the role local_area, and its builds
// are named by their folders. All values are synthetic.
const DENSE_OLDER_BUILD = `${DENSE_FOLDER}/uk-local-candidate-f100-s42-20261005T101500Z-0d0d0d0d`;
const DENSE_NEWER_BUILD = `${DENSE_FOLDER}/uk-local-candidate-f100-s42-20261019T000432Z-ee5c2dbe`;
const NATIONAL_BUILD = `${NATIONAL_FOLDER}/uk-frs-calibration-attempt-20261019T083323Z-6cba5d71`;

function recordedReleases() {
  const build = (publishedAt: string) => ({ published_at: publishedAt, withdrawn: false });
  return {
    schema_version: 1,
    default: NATIONAL_FOLDER,
    releases: {
      [NATIONAL_FOLDER]: {
        role: "national",
        current: NATIONAL_BUILD.split("/")[1],
        builds: { [NATIONAL_BUILD.split("/")[1]]: build("2026-10-19T08:33:23+00:00") },
      },
      [DENSE_FOLDER]: {
        role: "local_area",
        current: null,
        builds: {
          [DENSE_OLDER_BUILD.split("/")[1]]: build("2026-10-05T10:15:00+00:00"),
          [DENSE_NEWER_BUILD.split("/")[1]]: build("2026-10-19T00:04:32+00:00"),
        },
      },
    },
  };
}

function buildFolderManifest(): CalibrationTreeManifest {
  return [
    releaseBuild(NATIONAL_BUILD, "2026-10-19T08:33:23.000Z", "6"),
    releaseBuild(DENSE_OLDER_BUILD, "2026-10-05T10:15:00.000Z", "7"),
    releaseBuild(DENSE_NEWER_BUILD, "2026-10-19T00:04:32.000Z", "8"),
    // A tagged build published before the build folders, newer than both.
    releaseBuild(`${DENSE_FOLDER}-20261020T000000Z-ffffffff`, "2026-10-20T00:00:00.000Z", "9", DENSE_FOLDER),
  ].reduce(
    (manifest, entry) => withCalibrationTreeManifestEntry(manifest, "uk", entry),
    emptyCalibrationTreeManifest(),
  );
}

test("for a repository with releases.json, a local-area candidate's baseline is the newest published build of a local-area release it lists", async () => {
  const originalFetch = globalThis.fetch;
  const requested: string[] = [];
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    requested.push(url);
    if (url.endsWith("/resolve/main/releases.json")) return Response.json(recordedReleases());
    throw new Error(`Unexpected URL ${url}`);
  }) as typeof fetch;
  try {
    const manifest = buildFolderManifest();
    const releases = await stagingBaselineReleases(0, "uk", manifest);
    expect(newestPublishedLocalAreaRelease(manifest, "uk", releases)?.releaseId)
      .toBe(DENSE_NEWER_BUILD);
    // Nothing but releases.json: no release-folder listing and no tags.
    expect(requested).toHaveLength(1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("a withdrawn build is never a local-area candidate's baseline", async () => {
  const record = recordedReleases();
  record.releases[DENSE_FOLDER].builds[DENSE_NEWER_BUILD.split("/")[1]].withdrawn = true;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    if (url.endsWith("/resolve/main/releases.json")) return Response.json(record);
    throw new Error(`Unexpected URL ${url}`);
  }) as typeof fetch;
  try {
    const manifest = buildFolderManifest();
    const releases = await stagingBaselineReleases(0, "uk", manifest);
    // The withdrawn build stays in the inventory, marked, as the picker shows it.
    expect(releases.find((release) => release.release_id === DENSE_NEWER_BUILD)?.withdrawn).toBe(true);
    expect(newestPublishedLocalAreaRelease(manifest, "uk", releases)?.releaseId)
      .toBe(DENSE_OLDER_BUILD);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("for a repository without releases.json, the baseline comes from its release folders and published tagged builds", async () => {
  const originalFetch = globalThis.fetch;
  const requested: string[] = [];
  const taggedDense = `${DENSE_FOLDER}-20261020T000000Z-ffffffff`;
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    requested.push(url);
    if (url.endsWith("/resolve/main/releases.json")) return missingReleasesRecord();
    if (url.endsWith("/tree/main/releases?recursive=true")) return Response.json([]);
    if (url.endsWith(`/resolve/${"9".repeat(40)}/releases/${DENSE_FOLDER}/release_manifest.json`)) {
      return Response.json({ dataset_role: "non_default_local_area", is_default: false });
    }
    throw new Error(`Unexpected URL ${url}`);
  }) as typeof fetch;
  try {
    const manifest = buildFolderManifest();
    const baseline = stagingBaselineReleases(0, "uk", manifest);
    await expect(baseline).resolves.toBeDefined();
    const releases = await baseline;
    // Builds in their own folders are listed only from releases.json, so the
    // tagged build published before them is the baseline.
    expect(newestPublishedLocalAreaRelease(manifest, "uk", releases)?.releaseId).toBe(taggedDense);
    expect(requested.some((url) => url.endsWith("/tree/main/releases?recursive=true"))).toBe(true);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("a releases.json the release writer would refuse leaves a local-area candidate without a baseline", async () => {
  const refused = recordedReleases();
  // A current build that is withdrawn is refused by the writer's rules.
  refused.releases[NATIONAL_FOLDER].builds[NATIONAL_BUILD.split("/")[1]].withdrawn = true;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input);
    if (url.endsWith("/resolve/main/releases.json")) return Response.json(refused);
    throw new Error(`Unexpected URL ${url}`);
  }) as typeof fetch;
  try {
    const manifest = buildFolderManifest();
    await expect(stagingBaselineReleases(0, "uk", manifest)).rejects.toThrow("is withdrawn");
    let currentReleaseRead = false;
    const comparison = resolveStagingComparisonRelease(LOCAL_CANDIDATE_RUN, "uk", {
      runPipelineId: async () => "uk-local-candidate",
      currentReleaseId: async () => {
        currentReleaseRead = true;
        return NATIONAL_BUILD;
      },
      newestLocalAreaReleaseId: async (country) =>
        newestPublishedLocalAreaRelease(
          manifest,
          country,
          await stagingBaselineReleases(0, country, manifest),
        )?.releaseId ?? null,
    });
    // The comparison fails: it is not made against the current release.
    await expect(comparison).rejects.toThrow("is withdrawn");
    expect(currentReleaseRead).toBe(false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("a staging candidate's target changes can be compared with a build in its own folder", async () => {
  const originalFetch = globalThis.fetch;
  const requested: string[] = [];
  const progressUrl = stagingResolveUrl(`runs/${LOCAL_CANDIDATE_RUN}/progress.json`, "uk");
  // Every repository read fails, so loading stops at its first read.
  globalThis.fetch = (async (input: string | URL | Request) => {
    requested.push(String(input));
    return new Response(null, { status: 503 });
  }) as typeof fetch;
  try {
    const failure = await loadStagingTargetChangeDataset(
      LOCAL_CANDIDATE_RUN,
      DENSE_NEWER_BUILD,
      "uk",
    ).then(() => null, (error: unknown) => error);
    // The build is accepted as the release to compare with: loading starts
    // with the candidate's progress file and stops at the stubbed failure.
    expect(failure).not.toBeInstanceOf(InvalidReleaseIdError);
    expect(String(failure)).toContain("Staging fetch failed 503");
    expect(requested).toEqual([progressUrl]);

    // A path below a build folder is still refused before any read.
    requested.length = 0;
    await expect(
      loadStagingTargetChangeDataset(LOCAL_CANDIDATE_RUN, `${DENSE_NEWER_BUILD}/extra`, "uk"),
    ).rejects.toBeInstanceOf(InvalidReleaseIdError);
    expect(requested).toEqual([]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
