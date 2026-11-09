import { afterEach, expect, spyOn, test } from "bun:test";

import recordedGoldens from "./fixtures/calibration-tree-golden.json";
import type { CalibrationTreeBundle } from "@policyengine/calibration-tree/calibration-tree-bundle";
import {
  emptyCalibrationTreeManifest,
  withCalibrationTreeManifestEntry,
  type CalibrationTreeManifestEntry,
} from "@policyengine/calibration-tree/calibration-tree-manifest";
import {
  COUNTRY_REGISTRY,
  countryPointerPath,
  countryRegistration,
} from "@policyengine/calibration-tree/countries";
import {
  goldenReleaseInputs,
  ukStagingRepositoryFiles,
  withRegisteredRepositories,
  type CalibrationTreeGoldenRecord,
  type GoldenReleaseInput,
} from "./test-support/calibration-tree-golden-cases";
import { missingReleasesRecord } from "./test-support/releases-record-responses";
import {
  reconcileReleaseBuilds,
  reconcileStagingBuilds,
  runPublisher,
  type CalibrationManifestStore,
  type ReconciliationReport,
} from "@policyengine/calibration-tree/publish-calibration-tree";

// The recorded releases are release folders: the repositories here have no
// releases.json, so they are read from their tags and pointer files.
//
// A repair rebuilds a published release whose index went missing and refuses
// any rebuilt index whose digest differs from the manifest entry. Seeding the
// entry with the recorded build id and index digest therefore proves, through
// the publisher's own Hugging Face reads and source hashing, that it rebuilds
// the recorded index byte for byte.
const recorded = recordedGoldens as CalibrationTreeGoldenRecord;
const MAIN_REVISION_SHA = "0123456789abcdef0123456789abcdef01234567";
// The commit of the tag a per-release pointer file names (the UK's).
const POINTER_TAG_SHA = "89abcdef0123456789abcdef0123456789abcdef";
// Generous, so the suite also passes on a heavily loaded machine.
const BUILD_TIMEOUT_MS = 300_000;
const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

type ReleaseCaseName = keyof ReturnType<typeof goldenReleaseInputs>;

// The dataset repository as the publisher reads it: one tagged release at
// its commit, which `latest.json` on the main branch also names. A country
// registered with its own pointer file (the UK's `latest-national.json`)
// must have it name a tag whose release manifest names it, so that file
// names a tag of the recorded release at another commit. The tag is not
// listed among the repository's refs, so reconciliation never publishes it.
function huggingFaceReleaseRepository(release: GoldenReleaseInput): typeof fetch {
  const repo = COUNTRY_REGISTRY[release.country].repo;
  const api = `https://huggingface.co/api/datasets/${repo}`;
  const resolveUrl = (revision: string, path: string) =>
    `https://huggingface.co/datasets/${repo}/resolve/${revision}/${path}`;
  const prefix = `releases/${release.releaseId}`;
  const publishedFiles = new Map<string, string>([
    ["calibration_diagnostics.json", release.diagnosticsText],
    ...(release.releaseManifestText == null
      ? []
      : [["release_manifest.json", release.releaseManifestText] as [string, string]]),
  ]);
  const responses = new Map<string, () => Response>([
    [
      `${api}/tree/main/releases?recursive=true`,
      () => Response.json([...publishedFiles.keys()].map((name) => ({
        type: "file",
        path: `${prefix}/${name}`,
      }))),
    ],
    [
      `${api}/refs`,
      () => Response.json({
        tags: [{ name: release.releaseId, targetCommit: release.hfCommitSha }],
      }),
    ],
    [`${api}/revision/main`, () => Response.json({ sha: MAIN_REVISION_SHA })],
    [resolveUrl(MAIN_REVISION_SHA, "releases.json"), missingReleasesRecord],
    [`${api}/revision/${release.releaseId}`, () => Response.json({ sha: release.hfCommitSha })],
  ]);
  const pointerPath = countryPointerPath(release.country);
  if (countryRegistration(release.country).pointer_path == null) {
    responses.set(
      resolveUrl(MAIN_REVISION_SHA, pointerPath),
      () => Response.json({ release_id: release.releaseId }),
    );
  } else {
    const pointerTag = `${release.releaseId}-20261019T083323Z-0a1b2c3d`;
    responses.set(resolveUrl(MAIN_REVISION_SHA, pointerPath), () => Response.json({
      schema_version: 1,
      release_id: release.releaseId,
      line: pointerPath.replace(/^latest-/, "").replace(/\.json$/, ""),
      revision: pointerTag,
    }));
    responses.set(`${api}/revision/${pointerTag}`, () => Response.json({ sha: POINTER_TAG_SHA }));
    responses.set(
      `${api}/tree/${POINTER_TAG_SHA}/releases/${pointerTag}?recursive=false`,
      () => new Response(null, { status: 404 }),
    );
    responses.set(
      `${api}/tree/${POINTER_TAG_SHA}/${prefix}?recursive=false`,
      () => Response.json([{ type: "file", path: `${prefix}/calibration_diagnostics.json` }]),
    );
    responses.set(
      resolveUrl(POINTER_TAG_SHA, `${prefix}/release_manifest.json`),
      () => Response.json({ build: { cut_tag: pointerTag } }),
    );
  }
  for (const [name, text] of publishedFiles) {
    responses.set(resolveUrl(release.hfCommitSha, `${prefix}/${name}`), () => new Response(text));
    responses.set(resolveUrl("main", `${prefix}/${name}`), () => new Response(text));
  }
  return (async (input: string | URL | Request) => {
    const url = String(input);
    const respond = responses.get(url);
    if (respond) return respond();
    if (url.startsWith(resolveUrl(release.hfCommitSha, `${prefix}/`))) {
      return new Response(null, { status: 404 });
    }
    throw new Error(`Unexpected Hugging Face request ${url}`);
  }) as typeof fetch;
}

function recordedReleaseEntry(
  release: GoldenReleaseInput,
  name: ReleaseCaseName,
): CalibrationTreeManifestEntry {
  const record = recorded.sources[name];
  const index = record.files.find((file) => file.part === "index");
  if (!index) throw new Error(`The ${name} record has no index.`);
  return {
    buildArtifactId: record.buildArtifactId,
    kind: "release",
    sourceId: release.releaseId,
    label: release.releaseId,
    releaseId: release.releaseId,
    stagingRunId: null,
    hfRepo: COUNTRY_REGISTRY[release.country].repo,
    hfCommitSha: release.hfCommitSha,
    treeSchemaVersion: 6,
    indexSha256: index.sha256,
    indexBytes: index.rawBytes,
    createdAt: release.publishedAt,
    updatedAt: release.publishedAt,
  };
}

// A published release whose stored index has gone missing.
function storeMissingIndex(
  release: GoldenReleaseInput,
  entry: CalibrationTreeManifestEntry,
): CalibrationManifestStore {
  return {
    readManifest: async () => withCalibrationTreeManifestEntry(
      emptyCalibrationTreeManifest(),
      release.country,
      entry,
      true,
    ),
    listBlobs: async () => new Map(),
    audit: async () => ({
      complete: false,
      repairable: true,
      reasons: ["index is missing"],
      index: null,
    }),
    updateManifest: async () => {
      throw new Error("A dry run must not write the manifest.");
    },
    uploadBundle: async () => {
      throw new Error("A dry run must not upload a bundle.");
    },
  };
}

const RELEASE_DESCRIPTIONS: Record<"us-release" | "uk-release" | "be-release", string> = {
  "us-release": "US legacy",
  "uk-release": "UK hierarchy",
  "be-release": "Belgium",
};

for (const [name, description] of Object.entries(RELEASE_DESCRIPTIONS) as Array<
  [keyof typeof RELEASE_DESCRIPTIONS, string]
>) {
  test(
    `a dry-run repair of the recorded ${description} release rebuilds its recorded index`,
    async () => {
      const release = goldenReleaseInputs()[name];
      const entry = recordedReleaseEntry(release, name);
      globalThis.fetch = huggingFaceReleaseRepository(release);

      const report = await withRegisteredRepositories(() =>
        reconcileReleaseBuilds(release.country, "unused-blob-token", true, storeMissingIndex(release, entry))
      );

      expect(report.outcomes).toEqual([{
        sourceId: release.releaseId,
        status: "repaired",
        buildArtifactId: recorded.sources[name].buildArtifactId,
        reason: "Dry run; no Blob writes performed.",
      }]);
    },
    BUILD_TIMEOUT_MS,
  );
}

test("a repair refuses a rebuilt index that differs from the recorded digest and records it as that release's failure", async () => {
  const release = goldenReleaseInputs()["uk-release"];
  const entry = { ...recordedReleaseEntry(release, "uk-release"), indexSha256: "0".repeat(64) };
  globalThis.fetch = huggingFaceReleaseRepository(release);

  const report = await withRegisteredRepositories(() =>
    reconcileReleaseBuilds(release.country, "unused-blob-token", true, storeMissingIndex(release, entry))
  );

  expect(report.outcomes).toEqual([{
    sourceId: release.releaseId,
    status: "failed",
    buildArtifactId: entry.buildArtifactId,
    reason: `Rebuilt calibration index differs from immutable manifest entry ${entry.buildArtifactId}.`,
  }]);
}, BUILD_TIMEOUT_MS);

// Two finalized UK staging runs. The newer one, which reconciliation reaches
// first, already has a manifest entry whose index has gone missing, and the
// digest it records is not what the builder now writes for that run: a
// staging tree published before a builder change, such as the September UK
// local-area trees before same-named areas at different geography levels
// got separate labels.
const REFUSED_STAGING_RUN = {
  runId: "uk-calibration-refused-20261004T000000Z",
  updatedAt: "2026-10-04T00:00:00+00:00",
};
const UNPUBLISHED_STAGING_RUN = {
  runId: "uk-calibration-unpublished-20261003T000000Z",
  updatedAt: "2026-10-03T00:00:00+00:00",
};
const REFUSED_BUILD_ID = "a".repeat(64);

function stagingRepositoryWithRefusedRepair(): {
  revisionSha: string;
  store: CalibrationManifestStore;
  staleEntry: CalibrationTreeManifestEntry;
} {
  const { revisionSha, files } = ukStagingRepositoryFiles([
    REFUSED_STAGING_RUN,
    UNPUBLISHED_STAGING_RUN,
  ]);
  globalThis.fetch = (async (input: string | URL | Request) => {
    const body = files.get(String(input));
    return body == null ? new Response(null, { status: 404 }) : new Response(body);
  }) as typeof fetch;
  const staleEntry: CalibrationTreeManifestEntry = {
    buildArtifactId: REFUSED_BUILD_ID,
    kind: "staging",
    sourceId: REFUSED_STAGING_RUN.runId,
    label: `${REFUSED_STAGING_RUN.runId}-candidate`,
    releaseId: null,
    stagingRunId: REFUSED_STAGING_RUN.runId,
    hfRepo: COUNTRY_REGISTRY.uk.staging.repo,
    hfCommitSha: revisionSha,
    treeSchemaVersion: 6,
    indexSha256: "0".repeat(64),
    indexBytes: 10,
    createdAt: "2026-10-04T00:00:00.000Z",
    updatedAt: "2026-10-04T00:00:00.000Z",
  };
  const store: CalibrationManifestStore = {
    readManifest: async () => withCalibrationTreeManifestEntry(
      emptyCalibrationTreeManifest(),
      "uk",
      staleEntry,
    ),
    listBlobs: async () => new Map(),
    audit: async () => ({
      complete: false,
      repairable: true,
      reasons: ["index is missing"],
      index: null,
    }),
    updateManifest: async () => {
      throw new Error("A dry run must not write the manifest.");
    },
    uploadBundle: async () => {
      throw new Error("A dry run must not upload a bundle.");
    },
  };
  return { revisionSha, store, staleEntry };
}

test(
  "a refused staging repair is recorded as that run's failure, and the runs after it are still published",
  async () => {
    const { revisionSha, store } = stagingRepositoryWithRefusedRepair();

    const report = await withRegisteredRepositories(() =>
      reconcileStagingBuilds("uk", revisionSha, "unused-blob-token", true, store)
    );

    expect(report.outcomes).toEqual([
      {
        sourceId: REFUSED_STAGING_RUN.runId,
        status: "failed",
        buildArtifactId: REFUSED_BUILD_ID,
        reason: `Rebuilt staging index differs from immutable manifest entry ${REFUSED_BUILD_ID}.`,
      },
      {
        sourceId: UNPUBLISHED_STAGING_RUN.runId,
        status: "published",
        buildArtifactId: expect.stringMatching(/^[0-9a-f]{64}$/),
        reason: "Dry run; no Blob writes performed.",
      },
    ]);
  },
  BUILD_TIMEOUT_MS,
);

test(
  "the publisher prints every staging outcome, then exits with an error naming the failed run",
  async () => {
    const { revisionSha, store } = stagingRepositoryWithRefusedRepair();
    const originalBlobToken = process.env.BLOB_READ_WRITE_TOKEN;
    process.env.BLOB_READ_WRITE_TOKEN = "unused-blob-token";
    const printed = spyOn(console, "log").mockImplementation(() => {});
    try {
      await expect(withRegisteredRepositories(() =>
        runPublisher(
          { country: "uk", mode: "reconcile-staging", hfCommitSha: revisionSha, dryRun: true },
          { store },
        )
      )).rejects.toThrow(
        `1 of 2 staging sources failed to reconcile: ${REFUSED_STAGING_RUN.runId}.`,
      );
      expect(printed).toHaveBeenCalledTimes(1);
      const report = JSON.parse(String(printed.mock.calls[0][0])) as ReconciliationReport;
      expect(report.outcomes.map((outcome) => [outcome.sourceId, outcome.status])).toEqual([
        [REFUSED_STAGING_RUN.runId, "failed"],
        [UNPUBLISHED_STAGING_RUN.runId, "published"],
      ]);
    } finally {
      printed.mockRestore();
      if (originalBlobToken == null) delete process.env.BLOB_READ_WRITE_TOKEN;
      else process.env.BLOB_READ_WRITE_TOKEN = originalBlobToken;
    }
  },
  BUILD_TIMEOUT_MS,
);

// A store for a reconciliation that writes (not a dry run). It records each
// upload, manifest write, listing and audit in the order the publisher makes
// them. Its manifest starts with the refused run's entry and takes each write
// through the same entry checks as the Blob manifest. A build audits as
// complete once it has been both uploaded and written to the manifest.
function recordingStagingStore(staleEntry: CalibrationTreeManifestEntry) {
  const operations: string[] = [];
  const uploadedBuildIds = new Set<string>();
  const writtenEntries: CalibrationTreeManifestEntry[] = [];
  let storedManifest = withCalibrationTreeManifestEntry(
    emptyCalibrationTreeManifest(),
    "uk",
    staleEntry,
  );
  const store: CalibrationManifestStore = {
    readManifest: async () => storedManifest,
    listBlobs: async () => {
      operations.push("list files");
      return new Map();
    },
    audit: async (entry) => {
      operations.push(`audit ${entry.stagingRunId}`);
      const stored = uploadedBuildIds.has(entry.buildArtifactId) &&
        writtenEntries.some((written) => written.buildArtifactId === entry.buildArtifactId);
      return stored
        ? { complete: true, repairable: true, reasons: [], index: null }
        : { complete: false, repairable: true, reasons: ["index is missing"], index: null };
    },
    updateManifest: async (entry) => {
      operations.push(`write manifest entry for ${entry.stagingRunId}`);
      storedManifest = withCalibrationTreeManifestEntry(storedManifest, "uk", entry);
      writtenEntries.push(entry);
      return storedManifest;
    },
    uploadBundle: async (bundle: CalibrationTreeBundle) => {
      operations.push(`upload ${bundle.index.build.sourceId}`);
      uploadedBuildIds.add(bundle.index.buildArtifactId);
      const storedFiles = bundle.files.map((file) => ({
        pathname: file.path,
        sha256: file.sha256,
        bytes: file.rawBytes,
        created: true,
      }));
      const indexPosition = bundle.files.findIndex((file) => file.part === "index");
      return { files: storedFiles, index: storedFiles[indexPosition] };
    },
  };
  return {
    store,
    operations,
    writtenEntries,
    storedManifest: () => storedManifest,
  };
}

test(
  "a reconciliation that writes publishes the run after a refused one and writes nothing for the refused run",
  async () => {
    const { revisionSha, staleEntry } = stagingRepositoryWithRefusedRepair();
    const recording = recordingStagingStore(staleEntry);

    const report = await withRegisteredRepositories(() =>
      reconcileStagingBuilds("uk", revisionSha, "unused-blob-token", false, recording.store)
    );

    const publishedOutcome = report.outcomes[1];
    expect(report.outcomes).toEqual([
      {
        sourceId: REFUSED_STAGING_RUN.runId,
        status: "failed",
        buildArtifactId: REFUSED_BUILD_ID,
        reason: `Rebuilt staging index differs from immutable manifest entry ${REFUSED_BUILD_ID}.`,
      },
      {
        sourceId: UNPUBLISHED_STAGING_RUN.runId,
        status: "published",
        buildArtifactId: expect.stringMatching(/^[0-9a-f]{64}$/),
        reason: null,
      },
    ]);
    // The refused run is audited and then left alone; the next run is
    // uploaded, registered, listed again and audited, in that order.
    expect(recording.operations).toEqual([
      "list files",
      `audit ${REFUSED_STAGING_RUN.runId}`,
      `upload ${UNPUBLISHED_STAGING_RUN.runId}`,
      `write manifest entry for ${UNPUBLISHED_STAGING_RUN.runId}`,
      "list files",
      `audit ${UNPUBLISHED_STAGING_RUN.runId}`,
    ]);
    expect(recording.writtenEntries.map((entry) => [entry.kind, entry.stagingRunId])).toEqual([
      ["staging", UNPUBLISHED_STAGING_RUN.runId],
    ]);
    // The manifest keeps the refused run's entry exactly as it was and gains
    // one entry, for the published run.
    const storedBuilds = recording.storedManifest().countries.uk?.builds ?? [];
    expect(storedBuilds).toHaveLength(2);
    expect(storedBuilds.find((entry) => entry.buildArtifactId === REFUSED_BUILD_ID))
      .toEqual(staleEntry);
    expect(storedBuilds.find((entry) => entry.stagingRunId === UNPUBLISHED_STAGING_RUN.runId)
      ?.buildArtifactId ?? null).toBe(publishedOutcome.buildArtifactId);
  },
  BUILD_TIMEOUT_MS,
);
