import { afterEach, expect, spyOn, test } from "bun:test";

import {
  migrateReleaseTimestamps,
  parsePublisherOptions,
  readRecordedCurrentBuild,
  reconcileReleaseBuilds,
  runPublisher,
} from "./publish-calibration-tree";
import {
  emptyCalibrationTreeManifest,
  withCalibrationTreeManifestEntry,
  type CalibrationTreeManifest,
  type CalibrationTreeManifestEntry,
} from "./calibration-tree-manifest";
import type { CalibrationTreeBundle } from "./calibration-tree-bundle";
import type { MicrocosmCountry } from "./countries";
import { missingReleasesRecord, missingRevision } from "./test-support/releases-record-responses";

// A dataset repository that publishes each build in its own folder,
// releases/<release>/<build>/, with releases.json as the only record of
// which build is current (microcosm-emitter#24). Microcosm's switch commit
// writes this layout; nothing does yet, so every value here is synthetic.
// The readers find the layout by finding releases.json, whatever the country.
const UK_REPO = "policyengine/populace-uk-private";
const BE_REPO = "policyengine/populace-be-private";
const API = `https://huggingface.co/api/datasets/${UK_REPO}`;
const RESOLVE = `https://huggingface.co/datasets/${UK_REPO}/resolve`;
const NATIONAL = "microcosm-uk-2024-25-national";
const DENSE = "microcosm-uk-2024-25-dense";
const NATIONAL_OLDER = "uk-frs-calibration-attempt-20261014T214443Z-d12ff9b3";
const NATIONAL_CURRENT = "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71";
const DENSE_BUILD = "uk-local-candidate-f100-s42-20261019T000432Z-ee5c2dbe";
// Listed, but its folder's release manifest names another build.
const MISLABELLED_BUILD = "uk-frs-calibration-attempt-20261001T000000Z-99999999";
// Listed, but its folder holds no calibration diagnostics.
const UNCALIBRATED_BUILD = "uk-frs-calibration-attempt-20261002T000000Z-88888888";
const MAIN_SHA = "5".repeat(40);

const folder = (release: string, build: string) => `${release}/${build}`;

// The commit that wrote each build folder. The migration copies builds that
// were published earlier, so a folder's commit date is not when its build
// was published; releases.json records that.
const FOLDER_COMMITS: Record<string, { sha: string; date: string }> = {
  [folder(NATIONAL, NATIONAL_OLDER)]: { sha: "1".repeat(40), date: "2026-10-25T09:00:00.000Z" },
  [folder(NATIONAL, NATIONAL_CURRENT)]: { sha: "2".repeat(40), date: "2026-10-25T09:00:00.000Z" },
  [folder(DENSE, DENSE_BUILD)]: { sha: "3".repeat(40), date: "2026-10-25T09:05:00.000Z" },
  [folder(NATIONAL, MISLABELLED_BUILD)]: { sha: "4".repeat(40), date: "2026-10-25T09:00:00.000Z" },
  [folder(NATIONAL, UNCALIBRATED_BUILD)]: { sha: "6".repeat(40), date: "2026-10-25T09:00:00.000Z" },
};

const SCHEMA_8_DIAGNOSTICS = {
  schema_version: 8,
  targets: [{
    name: "provider.target@2025",
    target_name: "provider.target",
    source: "Citation",
    target: 100,
    initial_estimate: 90,
    final_estimate: 100,
    hierarchy: {
      provider: { id: "provider", label: "Provider label" },
      category: { id: "provider.category", label: "Category label", provider_id: "provider" },
      geography: { id: "K02000001", label: "United Kingdom", level: "country" },
      dimensions: [],
      target: { id: "provider.target", label: "Target label" },
    },
  }],
};

function releasesRecord(): Record<string, unknown> {
  const build = (publishedAt: string) => ({ published_at: publishedAt, withdrawn: false });
  return {
    schema_version: 1,
    default: NATIONAL,
    releases: {
      [NATIONAL]: {
        role: "national",
        current: NATIONAL_CURRENT,
        builds: {
          [NATIONAL_OLDER]: build("2026-10-14T21:44:43+00:00"),
          [NATIONAL_CURRENT]: build("2026-10-19T08:33:23+00:00"),
          [MISLABELLED_BUILD]: build("2026-10-01T00:00:00+00:00"),
          [UNCALIBRATED_BUILD]: build("2026-10-02T00:00:00+00:00"),
        },
      },
      [DENSE]: {
        role: "local_area",
        current: null,
        builds: { [DENSE_BUILD]: build("2026-10-19T07:53:18+00:00") },
      },
    },
  };
}

interface RepositoryOptions {
  /** The repository's releases.json; null when it has none. */
  record?: Record<string, unknown> | null;
  /** Hugging Face's answer for releases.json, in place of the record. */
  recordResponse?: () => Response;
  diagnosticsFor?: (folderId: string) => Record<string, unknown>;
  /** A build folder whose listing Hugging Face answers with a server error. */
  unreadableFolder?: string;
  /**
   * A build folder whose listing never answers: the request settles only
   * when its abort signal fires. Records the timeout the request asked for
   * (see withShortTimeouts).
   */
  stalledFolder?: { folderId: string; requestedTimeouts: Array<number | null> };
  /** The dataset repository; the UK's by default. */
  repo?: string;
  /** A build folder whose listing answers its first request, then 503. */
  folderReadableOnce?: string;
}

function buildFolderRepository(requested: string[], options: RepositoryOptions = {}) {
  const record = options.record === undefined ? releasesRecord() : options.record;
  const diagnosticsFor = options.diagnosticsFor ?? (() => SCHEMA_8_DIAGNOSTICS);
  const repo = options.repo ?? UK_REPO;
  const api = `https://huggingface.co/api/datasets/${repo}`;
  const resolve = `https://huggingface.co/datasets/${repo}/resolve`;
  let readableOnceListings = 0;
  return (async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    requested.push(url);
    if (url === `${api}/revision/main`) return Response.json({ sha: MAIN_SHA });
    if (url === `${resolve}/${MAIN_SHA}/releases.json`) {
      if (options.recordResponse) return options.recordResponse();
      return record == null ? missingReleasesRecord() : Response.json(record);
    }
    for (const [folderId, commit] of Object.entries(FOLDER_COMMITS)) {
      const [release, build] = folderId.split("/");
      const prefix = `releases/${folderId}`;
      if (url === `${api}/tree/${MAIN_SHA}/releases/${release}/${build}?recursive=false&expand=true`) {
        if (folderId === options.unreadableFolder) return new Response(null, { status: 503 });
        if (folderId === options.folderReadableOnce && readableOnceListings++ > 0) {
          return new Response(null, { status: 503 });
        }
        if (folderId === options.stalledFolder?.folderId) {
          const signal = init?.signal;
          options.stalledFolder.requestedTimeouts.push(
            (signal as TimedSignal | undefined)?.requestedTimeout ?? null,
          );
          if (!signal) throw new Error("The build folder listing was requested without a timeout.");
          return new Promise<Response>((_, reject) => {
            signal.addEventListener("abort", () => reject(signal.reason), { once: true });
          });
        }
        const names = build === UNCALIBRATED_BUILD
          ? ["release_manifest.json", "microcosm_uk_2024_25.h5"]
          : ["release_manifest.json", "calibration_diagnostics.json", "microcosm_uk_2024_25.h5"];
        return Response.json(names.map((name) => ({
          type: "file",
          path: `${prefix}/${name}`,
          lastCommit: { id: commit.sha, date: commit.date },
        })));
      }
      if (url === `${resolve}/${commit.sha}/${prefix}/release_manifest.json`) {
        return Response.json({
          release,
          build: build === MISLABELLED_BUILD ? NATIONAL_CURRENT : build,
          artifacts: {},
        });
      }
      if (url === `${resolve}/${commit.sha}/${prefix}/calibration_diagnostics.json`) {
        return Response.json(diagnosticsFor(folderId));
      }
      if (
        url === `${resolve}/${commit.sha}/${prefix}/build_manifest.json` ||
        url === `${resolve}/${commit.sha}/${prefix}/demographics.json`
      ) {
        return new Response(null, { status: 404 });
      }
    }
    throw new Error(`Unexpected URL ${url}`);
  }) as typeof fetch;
}

// A timeout signal that remembers the duration the code asked for.
type TimedSignal = AbortSignal & { requestedTimeout?: number };

// Every AbortSignal.timeout(...) fires after a few milliseconds instead, so a
// stalled request settles at once; each signal records the duration asked for.
async function withShortTimeouts<T>(run: () => Promise<T>): Promise<T> {
  const originalTimeout = AbortSignal.timeout.bind(AbortSignal);
  const timeouts = spyOn(AbortSignal, "timeout").mockImplementation((milliseconds: number) => {
    const signal: TimedSignal = originalTimeout(5);
    signal.requestedTimeout = milliseconds;
    return signal;
  });
  try {
    return await run();
  } finally {
    timeouts.mockRestore();
  }
}

function memoryStore(initial: CalibrationTreeManifest, country: MicrocosmCountry = "uk") {
  let manifest = initial;
  const uploads: CalibrationTreeBundle[] = [];
  const writes: Array<{ entry: CalibrationTreeManifestEntry; makeLatest?: boolean }> = [];
  return {
    uploads,
    writes,
    current: () => manifest,
    readManifest: async () => manifest,
    listBlobs: async () => new Map(),
    audit: async () => ({ complete: true, repairable: true, reasons: [], index: null }),
    updateManifest: async (entry: CalibrationTreeManifestEntry, makeLatest?: boolean) => {
      writes.push({ entry, makeLatest });
      manifest = withCalibrationTreeManifestEntry(manifest, country, entry, makeLatest);
      return manifest;
    },
    uploadBundle: async (bundle: CalibrationTreeBundle) => {
      uploads.push(bundle);
      const stored = (file: CalibrationTreeBundle["files"][number]) => ({
        pathname: file.path,
        sha256: file.sha256,
        bytes: file.rawBytes,
        created: true,
      });
      return {
        files: bundle.files.map(stored),
        index: stored(bundle.files.find((file) => file.part === "index")!),
      };
    },
  };
}

// Entries the publisher wrote before the build folders: a release folder's
// build and a tagged build of the national release, read from its folder.
const TAGGED_BUILD = `${NATIONAL}-20261019T083323Z-6cba5d71`;
function earlierEntries(): CalibrationTreeManifestEntry[] {
  const entry = (
    digit: string,
    releaseId: string,
    releaseDirectory?: string,
  ): CalibrationTreeManifestEntry => ({
    buildArtifactId: digit.repeat(64),
    kind: "release",
    sourceId: releaseId,
    label: releaseId,
    releaseId,
    ...(releaseDirectory ? { releaseDirectory } : {}),
    stagingRunId: null,
    hfRepo: UK_REPO,
    hfCommitSha: digit.repeat(40),
    treeSchemaVersion: 6,
    indexSha256: "e".repeat(64),
    indexBytes: 10,
    createdAt: "2026-10-19T10:21:22.000Z",
    updatedAt: "2026-10-19T10:21:22.000Z",
  });
  return [entry("a", "populace-uk-2023-test-20260619T023711Z"), entry("b", TAGGED_BUILD, NATIONAL)];
}

function manifestWithEarlierEntries(): CalibrationTreeManifest {
  const [june, tagged] = earlierEntries();
  return withCalibrationTreeManifestEntry(
    withCalibrationTreeManifestEntry(emptyCalibrationTreeManifest(), "uk", june),
    "uk",
    tagged,
    true,
  );
}

const originalFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("release reconciliation publishes every build releases.json lists, from its own folder at the commit that wrote it", async () => {
  const store = memoryStore(emptyCalibrationTreeManifest());
  const requested: string[] = [];
  globalThis.fetch = buildFolderRepository(requested);

  const report = await reconcileReleaseBuilds("uk", "blob-token", false, store);

  expect(Object.fromEntries(report.outcomes.map((outcome) => [outcome.sourceId, outcome.status])))
    .toEqual({
      [folder(DENSE, DENSE_BUILD)]: "published",
      [folder(NATIONAL, NATIONAL_OLDER)]: "published",
      [folder(NATIONAL, NATIONAL_CURRENT)]: "published",
      [folder(NATIONAL, MISLABELLED_BUILD)]: "ineligible",
      [folder(NATIONAL, UNCALIBRATED_BUILD)]: "ineligible",
    });
  const current = store.current().countries.uk!.builds
    .find((build) => build.releaseId === folder(NATIONAL, NATIONAL_CURRENT))!;
  // The build's folder is its release id; there is no separate directory.
  expect(current).toMatchObject({
    sourceId: folder(NATIONAL, NATIONAL_CURRENT),
    label: folder(NATIONAL, NATIONAL_CURRENT),
    hfCommitSha: FOLDER_COMMITS[folder(NATIONAL, NATIONAL_CURRENT)].sha,
  });
  expect(current.releaseDirectory).toBeUndefined();
  expect(requested).toContain(
    `${RESOLVE}/${FOLDER_COMMITS[folder(DENSE, DENSE_BUILD)].sha}/releases/${DENSE}/${DENSE_BUILD}/calibration_diagnostics.json`,
  );
});

test("a build whose folder cannot be read is reported as failed, and the other listed builds are still reconciled", async () => {
  const store = memoryStore(manifestWithEarlierEntries());
  globalThis.fetch = buildFolderRepository([], { unreadableFolder: folder(NATIONAL, NATIONAL_OLDER) });

  const reconciliation = reconcileReleaseBuilds("uk", "blob-token", false, store);
  await expect(reconciliation).resolves.toBeDefined();
  const report = await reconciliation;

  const failed = report.outcomes.find((outcome) => outcome.sourceId === folder(NATIONAL, NATIONAL_OLDER));
  expect(failed).toMatchObject({ status: "failed", buildArtifactId: null });
  expect(failed?.reason).toContain("503");
  expect(Object.fromEntries(report.outcomes.map((outcome) => [outcome.sourceId, outcome.status])))
    .toMatchObject({
      [folder(DENSE, DENSE_BUILD)]: "published",
      [folder(NATIONAL, NATIONAL_CURRENT)]: "published",
    });
  // The failed build is not the current one, so the latest release still moves.
  expect(report.latestReleaseFailure).toBeUndefined();
  const manifest = store.current().countries.uk!;
  expect(manifest.latestReleaseBuildArtifactId).toBe(
    manifest.builds.find((build) => build.releaseId === folder(NATIONAL, NATIONAL_CURRENT))!.buildArtifactId,
  );
});

test("a build folder listing that never answers times out after 30 seconds and fails only its own build", async () => {
  const store = memoryStore(manifestWithEarlierEntries());
  const requestedTimeouts: Array<number | null> = [];
  globalThis.fetch = buildFolderRepository([], {
    stalledFolder: { folderId: folder(NATIONAL, NATIONAL_OLDER), requestedTimeouts },
  });

  const report = await withShortTimeouts(() => reconcileReleaseBuilds("uk", "blob-token", false, store));

  expect(requestedTimeouts).toEqual([30_000]);
  const stalled = report.outcomes.find((outcome) => outcome.sourceId === folder(NATIONAL, NATIONAL_OLDER));
  expect(stalled?.status).toBe("failed");
  expect(stalled?.reason ?? "").toMatch(/timed out/i);
  expect(Object.fromEntries(report.outcomes.map((outcome) => [outcome.sourceId, outcome.status])))
    .toMatchObject({
      [folder(DENSE, DENSE_BUILD)]: "published",
      [folder(NATIONAL, NATIONAL_CURRENT)]: "published",
    });
  expect(report.latestReleaseFailure).toBeUndefined();
});

test("a build is dated by the publication time releases.json records, not by the commit that wrote its folder", async () => {
  const store = memoryStore(emptyCalibrationTreeManifest());
  globalThis.fetch = buildFolderRepository([]);

  await reconcileReleaseBuilds("uk", "blob-token", false, store);

  const older = store.current().countries.uk!.builds
    .find((build) => build.releaseId === folder(NATIONAL, NATIONAL_OLDER))!;
  expect(older.createdAt).toBe("2026-10-14T21:44:43.000Z");
  expect(older.updatedAt).toBe("2026-10-14T21:44:43.000Z");
});

test("the latest release becomes the current build of the record's default release", async () => {
  const store = memoryStore(manifestWithEarlierEntries());
  globalThis.fetch = buildFolderRepository([]);

  await reconcileReleaseBuilds("uk", "blob-token", false, store);

  const manifest = store.current().countries.uk!;
  const current = manifest.builds.find(
    (build) => build.releaseId === folder(NATIONAL, NATIONAL_CURRENT),
  )!;
  expect(manifest.latestReleaseBuildArtifactId).toBe(current.buildArtifactId);
});

test("reconciliation reads nothing but releases.json and the build folders it lists", async () => {
  const requested: string[] = [];
  globalThis.fetch = buildFolderRepository(requested);

  await reconcileReleaseBuilds(
    "uk",
    "blob-token",
    false,
    memoryStore(emptyCalibrationTreeManifest())
  );

  // No tag list, no pointer file, no listing of releases/ itself.
  expect(requested.some((url) => url === `${API}/refs`)).toBe(false);
  expect(requested.some((url) => /\/latest[^/]*\.json$/.test(url))).toBe(false);
  expect(requested.some((url) => url.includes("/tree/main/releases"))).toBe(false);
  for (const url of requested) {
    expect(
      url === `${API}/revision/main` ||
        url === `${RESOLVE}/${MAIN_SHA}/releases.json` ||
        /\/releases\/[^/]+\/[^/?]+(\/[^/]+)?(\?recursive=false&expand=true)?$/.test(url) ||
        /\/commits\//.test(url),
      url,
    ).toBe(true);
  }
});

test("entries published before the build folders stay in the dashboard manifest unchanged", async () => {
  const store = memoryStore(manifestWithEarlierEntries());
  globalThis.fetch = buildFolderRepository([]);

  const report = await reconcileReleaseBuilds("uk", "blob-token", false, store);

  const builds = store.current().countries.uk!.builds;
  for (const earlier of earlierEntries()) {
    expect(builds.find((build) => build.buildArtifactId === earlier.buildArtifactId)).toEqual(earlier);
    expect(report.outcomes.some((outcome) => outcome.sourceId === earlier.releaseId)).toBe(false);
  }
});

test("a second reconciliation finds every listed build complete without listing their folders again", async () => {
  const store = memoryStore(emptyCalibrationTreeManifest());
  globalThis.fetch = buildFolderRepository([]);
  await reconcileReleaseBuilds("uk", "blob-token", false, store);

  const requested: string[] = [];
  globalThis.fetch = buildFolderRepository(requested);
  const again = await reconcileReleaseBuilds("uk", "blob-token", false, store);

  expect(again.outcomes.filter((outcome) => outcome.status !== "ineligible")
    .every((outcome) => outcome.status === "complete")).toBe(true);
  expect(store.uploads).toHaveLength(3);
  for (const published of [NATIONAL_OLDER, NATIONAL_CURRENT]) {
    expect(requested.some((url) => url.includes(`/releases/${NATIONAL}/${published}?`))).toBe(false);
  }
});

test("a listed build is not published when its folder has no calibration diagnostics or its release manifest names another build", async () => {
  globalThis.fetch = buildFolderRepository([]);

  const report = await reconcileReleaseBuilds(
    "uk",
    "blob-token",
    false,
    memoryStore(emptyCalibrationTreeManifest())
  );

  const reason = (build: string) =>
    report.outcomes.find((outcome) => outcome.sourceId === folder(NATIONAL, build))?.reason ?? "";
  expect(reason(UNCALIBRATED_BUILD)).toContain("calibration diagnostics");
  expect(reason(MISLABELLED_BUILD)).toContain(
    `does not name release ${NATIONAL} and build ${MISLABELLED_BUILD}`,
  );
});

test("a build's comparison metadata names the build, not the release its diagnostics name", async () => {
  const store = memoryStore(emptyCalibrationTreeManifest());
  globalThis.fetch = buildFolderRepository([], {
    diagnosticsFor: (folderId) => ({ ...SCHEMA_8_DIAGNOSTICS, release_id: folderId.split("/")[0] }),
  });

  await reconcileReleaseBuilds("uk", "blob-token", false, store);

  expect(store.uploads).toHaveLength(3);
  for (const bundle of store.uploads) {
    expect(bundle.index.build.releaseId).not.toBeNull();
    expect(bundle.index.targetComparison.releaseId).toBe(bundle.index.build.releaseId ?? "");
  }
});

test("reconciliation refuses a releases.json the release writer would refuse, before publishing anything", async () => {
  const record = releasesRecord();
  const national = (record.releases as Record<string, Record<string, unknown>>)[NATIONAL];
  (national.builds as Record<string, Record<string, unknown>>)[NATIONAL_CURRENT].withdrawn = true;
  const store = memoryStore(emptyCalibrationTreeManifest());
  globalThis.fetch = buildFolderRepository([], { record });

  await expect(reconcileReleaseBuilds("uk", "blob-token", false, store))
    .rejects.toThrow("is withdrawn");
  expect(store.uploads).toHaveLength(0);
  expect(store.writes).toHaveLength(0);
});

// The writer's ReleasesRecord.parse refuses a record over MAX_RECORD_BYTES
// (4 MiB), the size check_change keeps every record within.
test("reconciliation refuses a releases.json larger than the release writer accepts, before publishing anything", async () => {
  // Trailing whitespace keeps the record otherwise valid.
  const oversized = JSON.stringify(releasesRecord()) + " ".repeat(4 * 1024 * 1024);
  const repository = buildFolderRepository([]);
  const store = memoryStore(emptyCalibrationTreeManifest());
  globalThis.fetch = (async (input: string | URL | Request) =>
    String(input) === `${RESOLVE}/${MAIN_SHA}/releases.json`
      ? new Response(oversized)
      : repository(input)) as typeof fetch;

  await expect(reconcileReleaseBuilds("uk", "blob-token", false, store))
    .rejects.toThrow("releases.json is larger than 4194304 bytes.");
  expect(store.uploads).toHaveLength(0);
  expect(store.writes).toHaveLength(0);
});

test("without a current build of the default release, reconciliation publishes the listed builds and fails without moving the latest release", async () => {
  const record = releasesRecord();
  (record.releases as Record<string, Record<string, unknown>>)[NATIONAL].current = null;
  const store = memoryStore(manifestWithEarlierEntries());
  const latestBefore = store.current().countries.uk!.latestReleaseBuildArtifactId;
  globalThis.fetch = buildFolderRepository([], { record });

  // Reconciliation reports why the latest release was not selected, and the
  // publisher then exits with an error (reconciliationFailureMessage).
  const report = await reconcileReleaseBuilds("uk", "blob-token", false, store);
  expect(report.latestReleaseFailure).toContain("names no current build of its default release");
  expect(store.uploads).toHaveLength(3);
  expect(store.current().countries.uk!.latestReleaseBuildArtifactId).toBe(latestBefore);
});

test("the current build is read from releases.json at the given commit and pinned to the commit that wrote its folder", async () => {
  const requested: string[] = [];
  globalThis.fetch = buildFolderRepository(requested);

  await expect(readRecordedCurrentBuild("uk", MAIN_SHA)).resolves.toEqual({
    releaseId: folder(NATIONAL, NATIONAL_CURRENT),
    releaseDirectory: folder(NATIONAL, NATIONAL_CURRENT),
    hfCommitSha: FOLDER_COMMITS[folder(NATIONAL, NATIONAL_CURRENT)].sha,
    publishedAt: "2026-10-19T08:33:23.000Z",
  });
  expect(requested).not.toContain(`${API}/revision/main`);
});

test("the publisher takes a build folder as the release to publish", () => {
  expect(parsePublisherOptions([
    "--country",
    "uk",
    "--release",
    folder(NATIONAL, NATIONAL_CURRENT),
  ])).toEqual({
    country: "uk",
    mode: "release",
    releaseId: folder(NATIONAL, NATIONAL_CURRENT),
  });
  for (const value of [`${NATIONAL}/${NATIONAL_CURRENT}/extra`, `${NATIONAL}/..`, `../${NATIONAL}`]) {
    expect(() => parsePublisherOptions(["--release", value]), value).toThrow(
      "valid immutable release id",
    );
  }
});

async function withBlobToken<T>(run: () => Promise<T>): Promise<T> {
  const priorBlobToken = process.env.BLOB_READ_WRITE_TOKEN;
  process.env.BLOB_READ_WRITE_TOKEN = "blob-token";
  try {
    return await run();
  } finally {
    if (priorBlobToken == null) delete process.env.BLOB_READ_WRITE_TOKEN;
    else process.env.BLOB_READ_WRITE_TOKEN = priorBlobToken;
  }
}

// The single-build modes print their result; the tests read the store.
async function quietly<T>(run: () => Promise<T>): Promise<T> {
  const printed = spyOn(console, "log").mockImplementation(() => {});
  try {
    return await run();
  } finally {
    printed.mockRestore();
  }
}

test("--latest publishes the current build of the default release and makes it the latest", async () => {
  const store = memoryStore(manifestWithEarlierEntries());
  globalThis.fetch = buildFolderRepository([]);

  await withBlobToken(() => quietly(() => runPublisher({ country: "uk", mode: "latest" }, { store })));

  const current = folder(NATIONAL, NATIONAL_CURRENT);
  expect(store.writes.map((write) => [write.entry.releaseId, write.makeLatest ?? false]))
    .toEqual([[current, false], [current, true]]);
  expect(store.writes[0].entry).toMatchObject({
    hfCommitSha: FOLDER_COMMITS[current].sha,
    createdAt: "2026-10-19T08:33:23.000Z",
  });
});

test("--release of another build publishes it without making it the latest or reading the current build's folder", async () => {
  const store = memoryStore(manifestWithEarlierEntries());
  const latestBefore = store.current().countries.uk!.latestReleaseBuildArtifactId;
  const requested: string[] = [];
  globalThis.fetch = buildFolderRepository(requested);

  await withBlobToken(() => quietly(() =>
    runPublisher({ country: "uk", mode: "release", releaseId: folder(NATIONAL, NATIONAL_OLDER) }, { store })
  ));

  expect(store.writes.map((write) => [write.entry.releaseId, write.makeLatest ?? false]))
    .toEqual([[folder(NATIONAL, NATIONAL_OLDER), false]]);
  expect(store.current().countries.uk!.latestReleaseBuildArtifactId).toBe(latestBefore);
  // The promotion check compares names from releases.json: the current
  // build's folder is never listed for it.
  expect(requested.some((url) => url.includes(`/releases/${NATIONAL}/${NATIONAL_CURRENT}`))).toBe(false);
});

test("--release of the current build still succeeds when its folder cannot be read again for the promotion check", async () => {
  const store = memoryStore(manifestWithEarlierEntries());
  const latestBefore = store.current().countries.uk!.latestReleaseBuildArtifactId;
  globalThis.fetch = buildFolderRepository([], { folderReadableOnce: folder(NATIONAL, NATIONAL_CURRENT) });
  const warned = spyOn(console, "warn").mockImplementation(() => {});

  try {
    const publication = withBlobToken(() => quietly(() =>
      runPublisher({ country: "uk", mode: "release", releaseId: folder(NATIONAL, NATIONAL_CURRENT) }, { store })
    ));
    await expect(publication).resolves.toBeUndefined();
    // The build is registered; reconciliation moves the latest release later.
    expect(store.writes.map((write) => [write.entry.releaseId, write.makeLatest ?? false]))
      .toEqual([[folder(NATIONAL, NATIONAL_CURRENT), false]]);
    expect(store.current().countries.uk!.latestReleaseBuildArtifactId).toBe(latestBefore);
    expect(warned.mock.calls.map((call) => call.map(String).join(" ")).join("\n"))
      .toContain("latest release was not moved");
  } finally {
    warned.mockRestore();
  }
});

test("when the current build's folder cannot be read, the latest-release failure names the read failure", async () => {
  const store = memoryStore(manifestWithEarlierEntries());
  const latestBefore = store.current().countries.uk!.latestReleaseBuildArtifactId;
  globalThis.fetch = buildFolderRepository([], { unreadableFolder: folder(NATIONAL, NATIONAL_CURRENT) });

  const report = await reconcileReleaseBuilds("uk", "blob-token", false, store);

  expect(report.outcomes.find((outcome) => outcome.sourceId === folder(NATIONAL, NATIONAL_CURRENT))?.status)
    .toBe("failed");
  expect(report.latestReleaseFailure ?? "").toContain(`Latest release ${folder(NATIONAL, NATIONAL_CURRENT)}`);
  expect(report.latestReleaseFailure ?? "").toContain("Hugging Face build folder lookup failed with 503.");
  expect(store.current().countries.uk!.latestReleaseBuildArtifactId).toBe(latestBefore);
});

test("when the current build is ineligible, the latest-release failure says why", async () => {
  const record = releasesRecord();
  (record.releases as Record<string, Record<string, unknown>>)[NATIONAL].current = UNCALIBRATED_BUILD;
  const store = memoryStore(manifestWithEarlierEntries());
  globalThis.fetch = buildFolderRepository([], { record });

  const report = await reconcileReleaseBuilds("uk", "blob-token", false, store);

  expect(report.latestReleaseFailure ?? "").toContain(
    `${folder(NATIONAL, UNCALIBRATED_BUILD)} cannot be published: releases/${folder(NATIONAL, UNCALIBRATED_BUILD)}/ has no calibration diagnostics.`,
  );
  expect(report.latestReleaseFailure ?? "").not.toContain("@ is not completely published");
});

test("a single-build publication refuses a build releases.json does not list, before reading any build file", async () => {
  const requested: string[] = [];
  globalThis.fetch = buildFolderRepository(requested);

  await withBlobToken(async () => {
    await expect(runPublisher(
      { country: "uk", mode: "release", releaseId: folder(NATIONAL, "uk-frs-calibration-attempt-unlisted") },
    )).rejects.toThrow("does not list build");
  });
  expect(requested.some((url) => url.includes("/tree/"))).toBe(false);
});

test("a single-build publication takes a build folder only from a repository with releases.json, and a tag only from one without", async () => {
  const withoutRecord: string[] = [];
  const withRecord: string[] = [];

  await withBlobToken(async () => {
    globalThis.fetch = buildFolderRepository(withoutRecord, { record: null });
    await expect(runPublisher(
      { country: "uk", mode: "release", releaseId: folder(NATIONAL, NATIONAL_CURRENT) },
    )).rejects.toThrow("has no releases.json");
    globalThis.fetch = buildFolderRepository(withRecord);
    await expect(runPublisher(
      { country: "uk", mode: "release", releaseId: TAGGED_BUILD },
    )).rejects.toThrow("has releases.json; --release takes <release>/<build>");
  });
  // Each refusal follows the one read that tells the layouts apart.
  for (const requested of [withoutRecord, withRecord]) {
    expect(requested).toEqual([`${API}/revision/main`, `${RESOLVE}/${MAIN_SHA}/releases.json`]);
  }
});

test("a repository with releases.json is reconciled from it, whichever country it belongs to", async () => {
  const store = memoryStore(emptyCalibrationTreeManifest(), "be");
  const requested: string[] = [];
  globalThis.fetch = buildFolderRepository(requested, { repo: BE_REPO });

  const reconciliation = reconcileReleaseBuilds("be", "blob-token", false, store);
  await expect(reconciliation).resolves.toBeDefined();
  const report = await reconciliation;

  expect(Object.fromEntries(report.outcomes.map((outcome) => [outcome.sourceId, outcome.status])))
    .toMatchObject({
      [folder(NATIONAL, NATIONAL_CURRENT)]: "published",
      [folder(DENSE, DENSE_BUILD)]: "published",
    });
  const manifest = store.current().countries.be!;
  expect(manifest.latestReleaseBuildArtifactId).toBe(
    manifest.builds.find((build) => build.releaseId === folder(NATIONAL, NATIONAL_CURRENT))!.buildArtifactId,
  );
  expect(requested.some((url) => url.endsWith("/refs"))).toBe(false);
});

test("a 404 that says the revision is missing is an error, not a repository without releases.json", async () => {
  const requested: string[] = [];
  globalThis.fetch = buildFolderRepository(requested, { repo: BE_REPO, recordResponse: missingRevision });

  await expect(reconcileReleaseBuilds("be", "blob-token", false, memoryStore(emptyCalibrationTreeManifest(), "be")))
    .rejects.toThrow("404");
  // Neither tags nor pointer files are read in its place.
  expect(requested.some((url) => url.endsWith("/refs"))).toBe(false);
  expect(requested.some((url) => /\/latest[^/]*\.json$/.test(url))).toBe(false);
});

test("the timestamp migration leaves build-folder entries dated as releases.json dated them", async () => {
  const store = memoryStore(emptyCalibrationTreeManifest());
  globalThis.fetch = buildFolderRepository([]);
  await reconcileReleaseBuilds("uk", "blob-token", false, store);
  const writesBefore = store.writes.length;

  const requested: string[] = [];
  globalThis.fetch = (async (input: string | URL | Request): Promise<Response> => {
    requested.push(String(input));
    throw new Error(`Unexpected URL ${String(input)}`);
  }) as typeof fetch;
  const report = await migrateReleaseTimestamps("uk", "blob-token", false, store);

  expect(report.outcomes).toEqual([]);
  expect(requested).toEqual([]);
  expect(store.writes).toHaveLength(writesBefore);
});
