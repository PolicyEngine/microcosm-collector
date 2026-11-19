// The command the collector's backfill uses to add copies of original files to
// builds the store already holds: it lists each build's stored copies, and
// stores the ones a build lacks, against an in-memory store that follows
// Vercel Blob's rule for allowOverwrite and pages its listings.
import { afterEach, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { gunzipSync, gzipSync } from "node:zlib";
import { BlobError } from "@vercel/blob";

const TOKEN = "vercel_blob_rw_test_token";
const TOKEN_VARIABLE = "CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN";
const BUILD_ID = "a".repeat(64);
const OTHER_BUILD_ID = "b".repeat(64);
const DIAGNOSTICS = new TextEncoder().encode('{"schema_version": 8, "targets": []}');
const BUILD_MANIFEST = new TextEncoder().encode('{"build": "us-calibration-attempt-1"}');
const RELEASE_MANIFEST = new TextEncoder().encode('{"release": "microcosm-us-2025"}');
const DEMOGRAPHICS = new TextEncoder().encode('{"households": 1}');
const PARENT_BUILD_MANIFEST = new TextEncoder().encode('{"build_id": "the-parent-build"}');
const REFORM_VALIDATION = new TextEncoder().encode('{"schema_version": 1, "reforms": []}');

async function copies() {
  const loaded = await import("./calibration-tree-source-copies").catch(() => null);
  expect(loaded).not.toBeNull();
  return loaded!;
}

const temporaryDirectories: string[] = [];

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function temporaryDirectory(): string {
  const directory = mkdtempSync(join(tmpdir(), "calibration-tree-source-copies-"));
  temporaryDirectories.push(directory);
  return directory;
}

function sha256(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function copyPath(country: string, buildArtifactId: string, name: string): string {
  return `calibration-tree-sources/${country}/${buildArtifactId}/${name}.gz`;
}

interface RecordedPut {
  pathname: string;
  options: Record<string, unknown>;
}

// Vercel Blob's rules: without allowOverwrite an existing blob is not
// replaced, and a listing is given in pages with a cursor.
class InMemoryBlobStore {
  readonly blobs = new Map<string, Uint8Array>();
  readonly puts: RecordedPut[] = [];
  readonly listings: Record<string, unknown>[] = [];
  pageSize = 1_000;
  failingGets = false;

  client() {
    const get = async (pathname: string) => {
      if (this.failingGets) throw new Error("The store is unavailable.");
      const bytes = this.blobs.get(pathname);
      if (!bytes) return null;
      return {
        statusCode: 200,
        stream: new Response(Buffer.from(bytes)).body,
        headers: new Headers(),
        blob: { pathname, etag: '"etag"', contentType: "application/octet-stream", size: bytes.byteLength },
      };
    };
    const put = async (pathname: string, body: Buffer, options: Record<string, unknown>) => {
      this.puts.push({ pathname, options });
      if (this.blobs.has(pathname) && !options.allowOverwrite) {
        throw new BlobError("This blob already exists, use `allowOverwrite: true` if you want to overwrite it.");
      }
      this.blobs.set(pathname, new Uint8Array(body));
      return { pathname };
    };
    const list = async (options: { prefix: string; cursor?: string; limit?: number; token?: string }) => {
      this.listings.push(options);
      const matching = [...this.blobs.keys()].filter((pathname) => pathname.startsWith(options.prefix)).sort();
      const offset = options.cursor ? Number(options.cursor) : 0;
      const page = matching.slice(offset, offset + this.pageSize);
      const hasMore = offset + this.pageSize < matching.length;
      return {
        blobs: page.map((pathname) => ({
          pathname,
          size: this.blobs.get(pathname)!.byteLength,
          url: `https://store.example.test/${pathname}`,
          downloadUrl: `https://store.example.test/${pathname}?download=1`,
          uploadedAt: new Date("2026-11-19T12:00:00Z"),
          etag: '"etag"',
        })),
        cursor: hasMore ? String(offset + this.pageSize) : undefined,
        hasMore,
      };
    };
    return { get, put, list } as never;
  }

  storeCopy(country: string, buildArtifactId: string, name: string, bytes: Uint8Array): void {
    this.blobs.set(copyPath(country, buildArtifactId, name), new Uint8Array(gzipSync(bytes)));
  }

  storeIndex(country: string, buildArtifactId: string, sourceArtifacts: Record<string, unknown>): void {
    this.blobs.set(
      `calibration-trees/${country}/${buildArtifactId}/index.json.gz`,
      new Uint8Array(gzipSync(JSON.stringify({ buildArtifactId, build: { sourceArtifacts } }))),
    );
  }

  copy(country: string, buildArtifactId: string, name: string): Uint8Array | null {
    const stored = this.blobs.get(copyPath(country, buildArtifactId, name));
    return stored ? new Uint8Array(gunzipSync(stored)) : null;
  }
}

function recordOf(path: string, bytes: Uint8Array) {
  return { path, sha256: sha256(bytes) };
}

/** The index of a release tree built from its diagnostics and manifests. */
const RECORDED_SOURCES = {
  calibrationDiagnostics: recordOf("releases/r/calibration_diagnostics.json", DIAGNOSTICS),
  buildManifest: recordOf("releases/r/build_manifest.json", BUILD_MANIFEST),
  releaseManifest: recordOf("releases/r/release_manifest.json", RELEASE_MANIFEST),
  demographics: recordOf("releases/r/demographics.json", DEMOGRAPHICS),
};

function requestWith(files: Array<[string, Uint8Array, string?]>, buildArtifactId = BUILD_ID): string {
  const directory = temporaryDirectory();
  const requestFiles = files.map(([file, bytes, digest], position) => {
    const path = join(directory, `${position}-${file}.json`);
    writeFileSync(path, bytes);
    return { file, path, sha256: digest ?? sha256(bytes) };
  });
  const requestPath = join(directory, "request.json");
  writeFileSync(requestPath, JSON.stringify({ country: "us", buildArtifactId, files: requestFiles }));
  return requestPath;
}

async function runCommand(
  argv: string[],
  store: InMemoryBlobStore,
  environment: Record<string, string | undefined> = { [TOKEN_VARIABLE]: TOKEN },
): Promise<{ exitCode: number; printed: unknown[]; errors: string[] }> {
  const { calibrationTreeSourceCopiesMain } = await copies();
  const printed: unknown[] = [];
  const errors: string[] = [];
  const log = console.log;
  const error = console.error;
  console.log = (text: string) => printed.push(JSON.parse(text));
  console.error = (text: string) => errors.push(String(text));
  try {
    const exitCode = await calibrationTreeSourceCopiesMain(argv, environment, store.client());
    return { exitCode, printed, errors };
  } finally {
    console.log = log;
    console.error = error;
  }
}

// Listing ------------------------------------------------------------------------

test("list prints the copies stored for each of the country's builds, across pages", async () => {
  const store = new InMemoryBlobStore();
  store.pageSize = 2;
  store.storeCopy("us", BUILD_ID, "calibration_diagnostics.json", DIAGNOSTICS);
  store.storeCopy("us", BUILD_ID, "reform_validation.json", REFORM_VALIDATION);
  store.storeCopy("us", OTHER_BUILD_ID, "build_manifest.json", BUILD_MANIFEST);
  store.storeCopy("uk", BUILD_ID, "demographics.json", DEMOGRAPHICS);
  // Neither is a copy this command knows of.
  store.blobs.set("calibration-tree-sources/us/not-a-build/build_manifest.json.gz", new Uint8Array(1));
  store.blobs.set(copyPath("us", BUILD_ID, "unknown.json"), new Uint8Array(1));

  const { exitCode, printed } = await runCommand(["list", "us"], store);

  expect(exitCode).toBe(0);
  expect(printed).toEqual([
    {
      builds: {
        [BUILD_ID]: ["calibration_diagnostics.json", "reform_validation.json"],
        [OTHER_BUILD_ID]: ["build_manifest.json"],
      },
    },
  ]);
  expect(store.listings.length).toBeGreaterThan(1);
  for (const listing of store.listings) {
    expect(listing.prefix).toBe("calibration-tree-sources/us/");
    expect(listing.token).toBe(TOKEN);
  }
  expect(store.puts).toEqual([]);
});

test("list prints no builds for a country with no copies", async () => {
  const { exitCode, printed } = await runCommand(["list", "be"], new InMemoryBlobStore());

  expect(exitCode).toBe(0);
  expect(printed).toEqual([{ builds: {} }]);
});

test("sources prints the files a stored tree records it was built from", async () => {
  const store = new InMemoryBlobStore();
  store.storeIndex("us", BUILD_ID, { ...RECORDED_SOURCES, demographics: null });

  const { exitCode, printed } = await runCommand(["sources", "us", BUILD_ID], store);

  expect(exitCode).toBe(0);
  expect(printed).toEqual([{ sources: { ...RECORDED_SOURCES, demographics: null } }]);
  expect(store.puts).toEqual([]);
});

test("sources exits 2 for a build whose tree the store does not hold", async () => {
  const { exitCode, errors } = await runCommand(["sources", "us", BUILD_ID], new InMemoryBlobStore());

  expect(exitCode).toBe(2);
  expect(errors.join("\n")).toContain(BUILD_ID);
});

test("sources needs a known country and a build ID", async () => {
  const store = new InMemoryBlobStore();

  expect((await runCommand(["sources", "fr", BUILD_ID], store)).exitCode).toBe(1);
  expect((await runCommand(["sources", "us", "../manifest"], store)).exitCode).toBe(1);
});

// Storing ------------------------------------------------------------------------

test("store uploads a gzip copy of each file the store lacks, under the stored build", async () => {
  const store = new InMemoryBlobStore();
  store.storeIndex("us", BUILD_ID, RECORDED_SOURCES);
  const request = requestWith([
    ["calibrationDiagnostics", DIAGNOSTICS],
    ["parentBuildManifest", PARENT_BUILD_MANIFEST],
    ["reformValidation", REFORM_VALIDATION],
  ]);

  const { exitCode, printed } = await runCommand(["store", request], store);

  expect(exitCode).toBe(0);
  expect(printed).toEqual([
    {
      uploaded: ["calibrationDiagnostics", "parentBuildManifest", "reformValidation"],
      kept: [],
      conflicts: [],
      refused: [],
    },
  ]);
  expect(store.copy("us", BUILD_ID, "calibration_diagnostics.json")).toEqual(DIAGNOSTICS);
  expect(store.copy("us", BUILD_ID, "parent_build_manifest.json")).toEqual(PARENT_BUILD_MANIFEST);
  expect(store.copy("us", BUILD_ID, "reform_validation.json")).toEqual(REFORM_VALIDATION);
  for (const recorded of store.puts) {
    expect(recorded.options.access).toBe("private");
    expect(recorded.options.addRandomSuffix).toBe(false);
    expect(recorded.options.allowOverwrite).toBeFalsy();
    expect(recorded.options.contentType).toBe("application/gzip");
    expect(recorded.options.token).toBe(TOKEN);
  }
  // Only copies are written: no part, index or manifest.
  expect(store.puts.every((recorded) => recorded.pathname.startsWith(`calibration-tree-sources/us/${BUILD_ID}/`)))
    .toBe(true);
});

test("a copy already stored is kept whatever it holds, and named when its bytes differ", async () => {
  const store = new InMemoryBlobStore();
  store.storeIndex("us", BUILD_ID, RECORDED_SOURCES);
  const otherParent = new TextEncoder().encode('{"build_id": "another parent"}');
  store.storeCopy("us", BUILD_ID, "parent_build_manifest.json", otherParent);
  store.storeCopy("us", BUILD_ID, "reform_validation.json", REFORM_VALIDATION);

  const { exitCode, printed } = await runCommand(
    [
      "store",
      requestWith([
        ["parentBuildManifest", PARENT_BUILD_MANIFEST],
        ["reformValidation", REFORM_VALIDATION],
      ]),
    ],
    store,
  );

  expect(exitCode).toBe(0);
  expect(printed).toEqual([
    { uploaded: [], kept: ["reformValidation"], conflicts: ["parentBuildManifest"], refused: [] },
  ]);
  expect(store.copy("us", BUILD_ID, "parent_build_manifest.json")).toEqual(otherParent);
  expect(store.puts).toEqual([]);
});

test("a tree file is stored only when the tree's index records it with the same SHA-256", async () => {
  const store = new InMemoryBlobStore();
  store.storeIndex("us", BUILD_ID, {
    calibrationDiagnostics: RECORDED_SOURCES.calibrationDiagnostics,
    buildManifest: recordOf("releases/r/build_manifest.json", new TextEncoder().encode("{}")),
    releaseManifest: null,
  });

  const { exitCode, printed } = await runCommand(
    [
      "store",
      requestWith([
        ["calibrationDiagnostics", DIAGNOSTICS],
        ["buildManifest", BUILD_MANIFEST],
        ["releaseManifest", RELEASE_MANIFEST],
        ["demographics", DEMOGRAPHICS],
      ]),
    ],
    store,
  );

  expect(exitCode).toBe(0);
  const [answer] = printed as Array<{ uploaded: string[]; refused: Array<{ file: string; reason: string }> }>;
  expect(answer.uploaded).toEqual(["calibrationDiagnostics"]);
  expect(answer.refused.map((refusal) => refusal.file)).toEqual(["buildManifest", "releaseManifest", "demographics"]);
  for (const refusal of answer.refused) expect(refusal.reason.length).toBeGreaterThan(0);
  expect(store.copy("us", BUILD_ID, "build_manifest.json")).toBeNull();
  expect(store.copy("us", BUILD_ID, "release_manifest.json")).toBeNull();
  expect(store.copy("us", BUILD_ID, "demographics.json")).toBeNull();
});

test("a tree file whose index records the SHA-256 of its decoded text is stored as its bytes", async () => {
  // The worker records a tree file's SHA-256 over its UTF-8 text, with a
  // leading byte-order mark dropped.
  const store = new InMemoryBlobStore();
  store.storeIndex("us", BUILD_ID, RECORDED_SOURCES);
  const withMark = new Uint8Array([0xef, 0xbb, 0xbf, ...DIAGNOSTICS]);

  const { exitCode, printed } = await runCommand(
    ["store", requestWith([["calibrationDiagnostics", withMark]])],
    store,
  );

  expect(exitCode).toBe(0);
  expect(printed).toEqual([{ uploaded: ["calibrationDiagnostics"], kept: [], conflicts: [], refused: [] }]);
  expect(store.copy("us", BUILD_ID, "calibration_diagnostics.json")).toEqual(withMark);
});

test("a build whose tree the store lacks is refused with exit 2 and nothing is stored", async () => {
  const store = new InMemoryBlobStore();

  const { exitCode, errors } = await runCommand(
    ["store", requestWith([["reformValidation", REFORM_VALIDATION]])],
    store,
  );

  expect(exitCode).toBe(2);
  expect(errors.join("\n")).toContain(BUILD_ID);
  expect(store.puts).toEqual([]);
});

test("a file on disk that differs from its SHA-256 stops the command before anything is stored", async () => {
  const store = new InMemoryBlobStore();
  store.storeIndex("us", BUILD_ID, RECORDED_SOURCES);

  const { exitCode } = await runCommand(
    [
      "store",
      requestWith([
        ["parentBuildManifest", PARENT_BUILD_MANIFEST],
        ["reformValidation", REFORM_VALIDATION, "f".repeat(64)],
      ]),
    ],
    store,
  );

  expect(exitCode).toBe(1);
  expect(store.puts).toEqual([]);
});

test("the command needs its token, a known action and a well-formed request", async () => {
  const store = new InMemoryBlobStore();
  store.storeIndex("us", BUILD_ID, RECORDED_SOURCES);
  const badBuild = requestWith([["reformValidation", REFORM_VALIDATION]], "../manifest");
  const unknownFile = requestWith([["calibrationTree", REFORM_VALIDATION]]);
  const repeatedFile = requestWith([
    ["reformValidation", REFORM_VALIDATION],
    ["reformValidation", REFORM_VALIDATION],
  ]);

  expect((await runCommand(["list", "us"], store, {})).exitCode).toBe(1);
  expect((await runCommand(["unknown"], store)).exitCode).toBe(1);
  expect((await runCommand(["list", "fr"], store)).exitCode).toBe(1);
  expect((await runCommand(["store", badBuild], store)).exitCode).toBe(1);
  expect((await runCommand(["store", unknownFile], store)).exitCode).toBe(1);
  expect((await runCommand(["store", repeatedFile], store)).exitCode).toBe(1);
  expect((await runCommand(["store", join(temporaryDirectory(), "absent.json")], store)).exitCode).toBe(1);
  expect(store.puts).toEqual([]);
});

test("a store that fails exits 3, so the backfill can try again", async () => {
  const store = new InMemoryBlobStore();
  store.failingGets = true;

  const { exitCode } = await runCommand(["store", requestWith([["reformValidation", REFORM_VALIDATION]])], store);

  expect(exitCode).toBe(3);
});
