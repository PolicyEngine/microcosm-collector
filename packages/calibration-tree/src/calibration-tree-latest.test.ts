// Pointing a country's latest release tree at a release build the manifest
// already records, for the collector's handler of releases.json changes. The
// latest follows releases.json whether that build is newer or older than the
// current latest; only the manifest is written, with a compare-and-swap; and
// a build the manifest doesn't record changes nothing. The store here is an
// in-memory stand-in that follows Vercel Blob's rules for allowOverwrite and
// ifMatch.
import { afterEach, expect, spyOn, test } from "bun:test";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { BlobError, BlobPreconditionFailedError } from "@vercel/blob";

import { CALIBRATION_TREE_SCHEMA_VERSION } from "./calibration-tree-artifact";
import type { CalibrationTreeBlobClient } from "./calibration-tree-blob";
import {
  CALIBRATION_TREE_MANIFEST_PATH,
  CALIBRATION_TREE_MANIFEST_SCHEMA_VERSION,
  parseCalibrationTreeManifest,
  type CalibrationTreeManifest,
  type CalibrationTreeManifestEntry,
} from "./calibration-tree-manifest";

const LATEST_COMMAND = join(import.meta.dir, "calibration-tree-latest.ts");
const TOKEN = "vercel_blob_rw_test_token";
const TOKEN_VARIABLE = "CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN";
const RELEASE = "microcosm-uk-2024-25-national";
const OLDER_BUILD = "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71";
const NEWER_BUILD = "uk-frs-calibration-attempt-20261101T000000Z-0a1b2c3d";
const OLDER_BUILD_ID = "a".repeat(64);
const NEWER_BUILD_ID = "b".repeat(64);
const STAGING_BUILD_ID = "c".repeat(64);

// The command's module, loaded by each test rather than imported at the top,
// so that while the command is missing each test fails on its first assertion.
async function latestCommand() {
  expect(existsSync(LATEST_COMMAND)).toBe(true);
  return await import("./calibration-tree-latest");
}

function releaseEntry(
  build: string,
  buildArtifactId: string,
  indexSha256: string,
  createdAt: string,
): CalibrationTreeManifestEntry {
  return {
    buildArtifactId,
    kind: "release",
    sourceId: `${RELEASE}/${build}`,
    label: `${RELEASE}/${build}`,
    releaseId: `${RELEASE}/${build}`,
    stagingRunId: null,
    hfRepo: "policyengine/populace-uk-private",
    hfCommitSha: "1".repeat(40),
    treeSchemaVersion: CALIBRATION_TREE_SCHEMA_VERSION,
    indexSha256,
    indexBytes: 1000,
    createdAt,
    updatedAt: createdAt,
  };
}

const OLDER_ENTRY = releaseEntry(OLDER_BUILD, OLDER_BUILD_ID, "d".repeat(64), "2026-10-19T09:00:00.000Z");
const NEWER_ENTRY = releaseEntry(NEWER_BUILD, NEWER_BUILD_ID, "e".repeat(64), "2026-11-01T09:00:00.000Z");
const STAGING_ENTRY: CalibrationTreeManifestEntry = {
  ...releaseEntry(NEWER_BUILD, STAGING_BUILD_ID, "f".repeat(64), "2026-11-01T08:00:00.000Z"),
  kind: "staging",
  sourceId: NEWER_BUILD,
  label: RELEASE,
  releaseId: null,
  stagingRunId: NEWER_BUILD,
  hfRepo: "policyengine/populace-uk-staging",
};

function ukManifest(latest: string | null): CalibrationTreeManifest {
  return {
    schemaVersion: CALIBRATION_TREE_MANIFEST_SCHEMA_VERSION,
    countries: {
      uk: { latestReleaseBuildArtifactId: latest, builds: [OLDER_ENTRY, STAGING_ENTRY, NEWER_ENTRY] },
    },
  };
}

interface StoredBlob {
  bytes: Uint8Array;
  etag: string;
}

interface RecordedPut {
  pathname: string;
  allowOverwrite: boolean | undefined;
  ifMatch: string | undefined;
  token: string | undefined;
}

// Vercel Blob's write rules: without allowOverwrite an existing blob is not
// replaced, and with a non-empty ifMatch a blob whose ETag differs is not
// replaced.
class InMemoryBlobStore {
  readonly blobs = new Map<string, StoredBlob>();
  readonly puts: RecordedPut[] = [];
  private written = 0;
  beforeManifestWrite: (() => void) | null = null;
  // Set to answer the manifest's reads with this ETag instead of its own.
  answeredManifestEtag: string | null = null;

  storeManifest(manifest: CalibrationTreeManifest): void {
    this.store(CALIBRATION_TREE_MANIFEST_PATH, new TextEncoder().encode(JSON.stringify(manifest)));
  }

  store(pathname: string, bytes: Uint8Array): void {
    this.written += 1;
    this.blobs.set(pathname, { bytes, etag: `"etag-${this.written}"` });
  }

  etag(): string | undefined {
    return this.blobs.get(CALIBRATION_TREE_MANIFEST_PATH)?.etag;
  }

  client(): CalibrationTreeBlobClient {
    const get = async (pathname: string) => {
      const stored = this.blobs.get(pathname);
      if (!stored) return null;
      return {
        statusCode: 200,
        stream: new Response(Buffer.from(stored.bytes)).body,
        headers: new Headers(),
        blob: {
          pathname,
          etag: this.answeredManifestEtag ?? stored.etag,
          contentType: "application/json",
          size: stored.bytes.byteLength,
        },
      };
    };
    const put = async (
      pathname: string,
      body: string | Buffer,
      options: { allowOverwrite?: boolean; ifMatch?: string; token?: string },
    ) => {
      this.puts.push({
        pathname,
        allowOverwrite: options.allowOverwrite,
        ifMatch: options.ifMatch,
        token: options.token,
      });
      if (pathname === CALIBRATION_TREE_MANIFEST_PATH) this.beforeManifestWrite?.();
      const existing = this.blobs.get(pathname);
      if (existing && !options.allowOverwrite) {
        throw new BlobError("This blob already exists, use `allowOverwrite: true` if you want to overwrite it.");
      }
      // As @vercel/blob does, an empty ifMatch sends no condition.
      if (options.ifMatch && existing?.etag !== options.ifMatch) {
        throw new BlobPreconditionFailedError();
      }
      this.store(pathname, typeof body === "string" ? new TextEncoder().encode(body) : new Uint8Array(body));
      return { pathname };
    };
    return { get, put } as unknown as CalibrationTreeBlobClient;
  }

  manifest(): CalibrationTreeManifest | null {
    const stored = this.blobs.get(CALIBRATION_TREE_MANIFEST_PATH);
    return stored ? parseCalibrationTreeManifest(JSON.parse(new TextDecoder().decode(stored.bytes))) : null;
  }
}

const temporaryDirectories: string[] = [];

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function requestFile(request: unknown): string {
  const directory = mkdtempSync(join(tmpdir(), "calibration-tree-latest-"));
  temporaryDirectories.push(directory);
  const path = join(directory, "latest.json");
  writeFileSync(path, typeof request === "string" ? request : JSON.stringify(request));
  return path;
}

const OLDER_REQUEST = { country: "uk", releaseId: `${RELEASE}/${OLDER_BUILD}` };
const NEWER_REQUEST = { country: "uk", releaseId: `${RELEASE}/${NEWER_BUILD}` };
const UNRECORDED_REQUEST = { country: "uk", releaseId: `${RELEASE}/uk-frs-calibration-attempt-other` };
const WITH_TOKEN = { [TOKEN_VARIABLE]: TOKEN };

test("an older recorded release build becomes the latest, as releases.json says", async () => {
  const latest = await latestCommand();
  const store = new InMemoryBlobStore();
  store.storeManifest(ukManifest(NEWER_BUILD_ID));
  const readEtag = store.etag();

  const result = await latest.moveCalibrationTreeLatest({
    request: latest.parseCalibrationTreeLatestRequest(OLDER_REQUEST),
    token: TOKEN,
    client: store.client(),
  });

  expect(result).toEqual({
    country: "uk",
    releaseId: `${RELEASE}/${OLDER_BUILD}`,
    buildArtifactId: OLDER_BUILD_ID,
    moved: true,
  });
  // Only the manifest is written, replacing exactly the version read.
  expect(store.puts).toEqual([
    { pathname: CALIBRATION_TREE_MANIFEST_PATH, allowOverwrite: true, ifMatch: readEtag, token: TOKEN },
  ]);
  // The same builds, which the manifest's serializer stores sorted by build
  // ID; only the latest changed.
  const written = ukManifest(NEWER_BUILD_ID);
  written.countries.uk!.latestReleaseBuildArtifactId = OLDER_BUILD_ID;
  written.countries.uk!.builds.sort((left, right) => left.buildArtifactId.localeCompare(right.buildArtifactId));
  expect(store.manifest()).toEqual(written);
});

test("a newer recorded release build becomes the latest too", async () => {
  const latest = await latestCommand();
  const store = new InMemoryBlobStore();
  store.storeManifest(ukManifest(OLDER_BUILD_ID));

  const result = await latest.moveCalibrationTreeLatest({
    request: latest.parseCalibrationTreeLatestRequest(NEWER_REQUEST),
    token: TOKEN,
    client: store.client(),
  });

  expect(result.moved).toBe(true);
  expect(store.manifest()?.countries.uk?.latestReleaseBuildArtifactId).toBe(NEWER_BUILD_ID);
});

test("a country without a latest gets one", async () => {
  const latest = await latestCommand();
  const store = new InMemoryBlobStore();
  store.storeManifest(ukManifest(null));

  const result = await latest.moveCalibrationTreeLatest({
    request: latest.parseCalibrationTreeLatestRequest(OLDER_REQUEST),
    token: TOKEN,
    client: store.client(),
  });

  expect(result.moved).toBe(true);
  expect(store.manifest()?.countries.uk?.latestReleaseBuildArtifactId).toBe(OLDER_BUILD_ID);
});

test("a build that already is the latest is left as it is, and nothing is written", async () => {
  const latest = await latestCommand();
  const store = new InMemoryBlobStore();
  store.storeManifest(ukManifest(OLDER_BUILD_ID));

  const result = await latest.moveCalibrationTreeLatest({
    request: latest.parseCalibrationTreeLatestRequest(OLDER_REQUEST),
    token: TOKEN,
    client: store.client(),
  });

  expect(result).toEqual({
    country: "uk",
    releaseId: `${RELEASE}/${OLDER_BUILD}`,
    buildArtifactId: OLDER_BUILD_ID,
    moved: false,
  });
  expect(store.puts).toEqual([]);
});

const NOT_RECORDED_CASES: Array<[string, CalibrationTreeManifest | null, unknown]> = [
  ["no manifest", null, OLDER_REQUEST],
  [
    "no builds of the country",
    { schemaVersion: CALIBRATION_TREE_MANIFEST_SCHEMA_VERSION, countries: {} },
    OLDER_REQUEST,
  ],
  ["another build of the release", ukManifest(OLDER_BUILD_ID), UNRECORDED_REQUEST],
  ["the build in another country", ukManifest(OLDER_BUILD_ID), { country: "us", releaseId: `${RELEASE}/${OLDER_BUILD}` }],
];

test.each(NOT_RECORDED_CASES)(
  "a release build the manifest doesn't record changes nothing (%s)",
  async (_case, stored, request) => {
    const latest = await latestCommand();
    const store = new InMemoryBlobStore();
    if (stored) store.storeManifest(stored);
    const before = store.manifest();

    const moving = latest.moveCalibrationTreeLatest({
      request: latest.parseCalibrationTreeLatestRequest(request),
      token: TOKEN,
      client: store.client(),
    });

    await expect(moving).rejects.toBeInstanceOf(latest.CalibrationTreeNotRecordedError);
    expect(store.puts).toEqual([]);
    expect(store.manifest()).toEqual(before);
    expect(latest.calibrationTreeLatestExitCode(await moving.catch((error: unknown) => error))).toBe(2);
  },
);

test("a manifest another writer changed after it was read is not overwritten", async () => {
  const latest = await latestCommand();
  const store = new InMemoryBlobStore();
  store.storeManifest(ukManifest(NEWER_BUILD_ID));
  const otherWritersManifest = ukManifest(null);
  store.beforeManifestWrite = () => store.storeManifest(otherWritersManifest);

  const moving = latest.moveCalibrationTreeLatest({
    request: latest.parseCalibrationTreeLatestRequest(OLDER_REQUEST),
    token: TOKEN,
    client: store.client(),
  });

  const error = await moving.catch((caught: unknown) => caught);
  expect(error).toBeInstanceOf(Error);
  expect(error).not.toBeInstanceOf(latest.CalibrationTreeNotRecordedError);
  expect(latest.calibrationTreeLatestExitCode(error)).toBe(3);
  expect(store.manifest()).toEqual(otherWritersManifest);
});

test("a manifest read without an ETag is never replaced", async () => {
  const latest = await latestCommand();
  const store = new InMemoryBlobStore();
  store.storeManifest(ukManifest(NEWER_BUILD_ID));
  store.answeredManifestEtag = "";

  const moving = latest.moveCalibrationTreeLatest({
    request: latest.parseCalibrationTreeLatestRequest(OLDER_REQUEST),
    token: TOKEN,
    client: store.client(),
  });

  const error = await moving.catch((caught: unknown) => caught);
  expect(latest.calibrationTreeLatestExitCode(error)).toBe(3);
  expect(store.puts).toEqual([]);
  expect(store.manifest()?.countries.uk?.latestReleaseBuildArtifactId).toBe(NEWER_BUILD_ID);
});

const REFUSED_REQUESTS: Array<[string, unknown]> = [
  ["not an object", ["uk", `${RELEASE}/${OLDER_BUILD}`]],
  ["an unknown field", { ...OLDER_REQUEST, makeLatest: true }],
  ["no release ID", { country: "uk" }],
  ["a country not registered", { country: "fr", releaseId: `${RELEASE}/${OLDER_BUILD}` }],
  ["a release without a build", { country: "uk", releaseId: RELEASE }],
  ["three segments", { country: "uk", releaseId: `${RELEASE}/${OLDER_BUILD}/extra` }],
  ["a segment that leaves its folder", { country: "uk", releaseId: `../${OLDER_BUILD}` }],
  ["an empty release ID", { country: "uk", releaseId: "" }],
];

test.each(REFUSED_REQUESTS)("a request with %s is refused", async (_case, request) => {
  const latest = await latestCommand();

  expect(() => latest.parseCalibrationTreeLatestRequest(request)).toThrow(latest.CalibrationTreeLatestRequestError);
});

test("the command prints one JSON line and exits 0 once the latest is moved", async () => {
  const latest = await latestCommand();
  const store = new InMemoryBlobStore();
  store.storeManifest(ukManifest(NEWER_BUILD_ID));
  const printed = spyOn(console, "log").mockImplementation(() => {});

  try {
    const exitCode = await latest.calibrationTreeLatestMain(
      [requestFile(OLDER_REQUEST)],
      WITH_TOKEN,
      store.client(),
    );

    expect(exitCode).toBe(0);
    expect(printed.mock.calls.map((call) => JSON.parse(String(call[0])))).toEqual([
      { country: "uk", releaseId: `${RELEASE}/${OLDER_BUILD}`, buildArtifactId: OLDER_BUILD_ID, moved: true },
    ]);
  } finally {
    printed.mockRestore();
  }
});

// Each case's arguments, made when the case runs.
const COMMAND_EXITS: Array<[string, () => string[], Record<string, string>, number]> = [
  ["no token", () => [requestFile(OLDER_REQUEST)], {}, 1],
  ["no request", () => [], WITH_TOKEN, 1],
  ["two requests", () => [requestFile(OLDER_REQUEST), requestFile(OLDER_REQUEST)], WITH_TOKEN, 1],
  ["a request that is not JSON", () => [requestFile("not json")], WITH_TOKEN, 1],
  ["a request the rules refuse", () => [requestFile({ country: "uk", releaseId: RELEASE })], WITH_TOKEN, 1],
  ["a build the manifest doesn't record", () => [requestFile(UNRECORDED_REQUEST)], WITH_TOKEN, 2],
];

test.each(COMMAND_EXITS)("the command exits with its code for %s", async (_case, commandArguments, environment, expected) => {
  const latest = await latestCommand();
  const store = new InMemoryBlobStore();
  store.storeManifest(ukManifest(NEWER_BUILD_ID));
  const reported = spyOn(console, "error").mockImplementation(() => {});

  try {
    const exitCode = await latest.calibrationTreeLatestMain(commandArguments(), environment, store.client());

    expect(exitCode).toBe(expected);
    expect(reported).toHaveBeenCalledTimes(1);
    expect(store.puts).toEqual([]);
  } finally {
    reported.mockRestore();
  }
});
