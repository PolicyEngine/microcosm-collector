// The collector's stored copy of a country's releases.json: the document it
// keeps in the tree store, the package's reader, and the command that replaces
// the copy only if nobody else changed it first.
import { afterEach, expect, spyOn, test } from "bun:test";
import { BlobError, BlobPreconditionFailedError } from "@vercel/blob";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import type { CalibrationTreeBlobClient } from "./calibration-tree-blob";
import { calibrationTreeSourceFilePath } from "./calibration-tree-source-files";

const READER_MODULE = join(import.meta.dir, "calibration-tree-releases-record.ts");
const COPY_COMMAND = join(import.meta.dir, "calibration-tree-releases-record-copy.ts");
const TOKEN = "vercel_blob_rw_test_token";
const TOKEN_VARIABLE = "CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN";
const WITH_TOKEN = { [TOKEN_VARIABLE]: TOKEN };
const REPO = "policyengine/populace-us";
const COMMIT = "1".repeat(40);
const NEWER_COMMIT = "2".repeat(40);
const COPIED_AT = "2026-11-19T12:00:00.000Z";
const NOW = () => new Date(COPIED_AT);
const STORED_PATH = "calibration-tree-sources/us/releases-record.json";
const RELEASE = "microcosm-us-2025";
const BUILD = "us-calibration-attempt-20261101T120000Z-0a1b2c3d";

// While a module is missing, each test fails on its first assertion.
async function readerModule() {
  expect(existsSync(READER_MODULE)).toBe(true);
  return await import("./calibration-tree-releases-record");
}

async function copyCommand() {
  expect(existsSync(COPY_COMMAND)).toBe(true);
  return await import("./calibration-tree-releases-record-copy");
}

function recordBytes(current: string | null = BUILD): Uint8Array {
  const document = {
    schema_version: 1,
    default: RELEASE,
    releases: {
      [RELEASE]: {
        role: "national",
        current,
        builds: { [BUILD]: { published_at: "2026-11-01T12:00:00Z", withdrawn: false } },
      },
    },
  };
  // Spacing the parser ignores, so a test can tell the exact bytes were kept.
  return new TextEncoder().encode(`${JSON.stringify(document, null, 3)}\n`);
}

function sha256(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function encoded(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

function decoded(bytes: Uint8Array): string {
  return new TextDecoder("utf-8", { ignoreBOM: true }).decode(bytes);
}

function storedDocument(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  const bytes = recordBytes();
  return {
    schemaVersion: 1,
    country: "us",
    hfRepo: REPO,
    hfCommitSha: COMMIT,
    copiedAt: COPIED_AT,
    record: { sha256: sha256(bytes), bytes: bytes.byteLength, text: decoded(bytes) },
    ...overrides,
  };
}

interface RecordedGet {
  pathname: string;
  access: unknown;
  useCache: unknown;
  acceptEncoding: string | undefined;
  token: unknown;
}

interface RecordedPut {
  pathname: string;
  options: Record<string, unknown>;
}

// Vercel Blob's rules: without allowOverwrite an existing blob is not
// replaced, and a blob whose ETag differs from ifMatch is not replaced.
class InMemoryBlobStore {
  readonly blobs = new Map<string, { bytes: Uint8Array; etag: string }>();
  readonly gets: RecordedGet[] = [];
  readonly puts: RecordedPut[] = [];
  beforeWrite: (() => void) | null = null;
  writeError: Error | null = null;
  ignoresWrites = false;
  answeredEtag: string | null = null;
  compressesReads = false;
  answeredStatus = 200;
  private written = 0;

  store(pathname: string, bytes: Uint8Array): string {
    this.written += 1;
    const etag = `"etag-${this.written}"`;
    this.blobs.set(pathname, { bytes, etag });
    return etag;
  }

  storeDocument(document: unknown = storedDocument()): string {
    return this.store(STORED_PATH, encoded(typeof document === "string" ? document : JSON.stringify(document)));
  }

  document(): Record<string, any> {
    return JSON.parse(decoded(this.blobs.get(STORED_PATH)!.bytes));
  }

  client(): CalibrationTreeBlobClient {
    const get = async (pathname: string, options: Record<string, any> = {}) => {
      const acceptEncoding = options.headers?.["accept-encoding"];
      this.gets.push({
        pathname,
        access: options.access,
        useCache: options.useCache,
        acceptEncoding,
        token: options.token,
      });
      const stored = this.blobs.get(pathname);
      if (!stored) return null;
      const etag = this.answeredEtag ?? stored.etag;
      const compressed = this.compressesReads && acceptEncoding !== "identity";
      return {
        statusCode: this.answeredStatus,
        stream: this.answeredStatus === 200 ? new Response(Buffer.from(stored.bytes)).body : null,
        headers: new Headers(),
        blob: { pathname, etag: compressed ? `W/${etag}` : etag, contentType: "application/json", size: stored.bytes.byteLength },
      };
    };
    const put = async (pathname: string, body: string, options: Record<string, any>) => {
      this.puts.push({ pathname, options });
      const hook = this.beforeWrite;
      this.beforeWrite = null;
      hook?.();
      if (this.writeError) throw this.writeError;
      const existing = this.blobs.get(pathname);
      if (existing && !options.allowOverwrite) {
        throw new BlobError("This blob already exists, use `allowOverwrite: true` if you want to overwrite it.");
      }
      if (options.ifMatch && existing?.etag !== options.ifMatch) throw new BlobPreconditionFailedError();
      if (this.ignoresWrites) return { pathname, etag: '"never-stored"' };
      return { pathname, etag: this.store(pathname, encoded(body)) };
    };
    return { get, put } as unknown as CalibrationTreeBlobClient;
  }
}

const temporaryDirectories: string[] = [];

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function temporaryFile(name: string, contents: string | Uint8Array): string {
  const directory = mkdtempSync(join(tmpdir(), "calibration-tree-releases-record-"));
  temporaryDirectories.push(directory);
  const filePath = join(directory, name);
  writeFileSync(filePath, contents);
  return filePath;
}

function copyRequest(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    country: "us",
    hfRepo: REPO,
    hfCommitSha: COMMIT,
    expectedEtag: null,
    recordFile: temporaryFile("releases.json", recordBytes()),
    ...overrides,
  };
}

async function writeCopy(store: InMemoryBlobStore, overrides: Record<string, unknown> = {}) {
  const command = await copyCommand();
  return command.writeReleasesRecordCopy({
    request: command.parseReleasesRecordCopyRequest(copyRequest(overrides)),
    token: TOKEN,
    client: store.client(),
    now: NOW,
  });
}

// Where the copy lives -----------------------------------------------------------

test("each country's copy has one path, beside its builds' original files", async () => {
  const { storedReleasesRecordPath } = await readerModule();

  expect(storedReleasesRecordPath("us")).toBe(STORED_PATH);
  expect(storedReleasesRecordPath("uk")).toBe("calibration-tree-sources/uk/releases-record.json");
  expect(storedReleasesRecordPath("be")).toBe("calibration-tree-sources/be/releases-record.json");
  const buildFile = calibrationTreeSourceFilePath("us", "a".repeat(64), "demographics");
  expect(buildFile.startsWith("calibration-tree-sources/us/")).toBe(true);
  expect(buildFile.startsWith(`${STORED_PATH}/`)).toBe(false);
  expect(() => storedReleasesRecordPath("fr" as never)).toThrow();
});

// The reader ---------------------------------------------------------------------

test("a country the collector has not copied reads as not_copied", async () => {
  const { readStoredReleasesRecord } = await readerModule();
  const store = new InMemoryBlobStore();

  await expect(readStoredReleasesRecord({ country: "us", token: TOKEN, client: store.client() }))
    .resolves.toEqual({ state: "not_copied" });
});

test("a stored record is read with the rules of a releases.json, with its commit", async () => {
  const { readStoredReleasesRecord } = await readerModule();
  const store = new InMemoryBlobStore();
  store.storeDocument();

  const stored = await readStoredReleasesRecord({ country: "us", token: TOKEN, client: store.client() });

  expect(stored).toEqual({
    state: "record",
    country: "us",
    hfRepo: REPO,
    hfCommitSha: COMMIT,
    copiedAt: COPIED_AT,
    sha256: sha256(recordBytes()),
    record: {
      default: RELEASE,
      releases: {
        [RELEASE]: {
          role: "national",
          current: BUILD,
          builds: { [BUILD]: { publishedAt: "2026-11-01T12:00:00Z", withdrawn: false } },
        },
      },
    },
  });
});

test("a copy saying the repo had no releases.json reads as no_record, not as not_copied", async () => {
  const { readStoredReleasesRecord } = await readerModule();
  const store = new InMemoryBlobStore();
  store.storeDocument(storedDocument({ record: null }));

  await expect(readStoredReleasesRecord({ country: "us", token: TOKEN, client: store.client() }))
    .resolves.toEqual({
      state: "no_record",
      country: "us",
      hfRepo: REPO,
      hfCommitSha: COMMIT,
      copiedAt: COPIED_AT,
    });
});

test("the reader reads privately, from the cache unless asked to be consistent", async () => {
  const { readStoredReleasesRecord } = await readerModule();
  const store = new InMemoryBlobStore();
  store.storeDocument();

  await readStoredReleasesRecord({ country: "us", token: TOKEN, client: store.client() });
  await readStoredReleasesRecord({ country: "us", token: TOKEN, consistent: true, client: store.client() });

  expect(store.gets).toEqual([
    { pathname: STORED_PATH, access: "private", useCache: true, acceptEncoding: undefined, token: TOKEN },
    { pathname: STORED_PATH, access: "private", useCache: false, acceptEncoding: undefined, token: TOKEN },
  ]);
});

const BAD_DOCUMENTS: Array<[string, unknown]> = [
  ["text that is not JSON", "not json"],
  ["a list", []],
  ["another schema version", storedDocument({ schemaVersion: 2 })],
  ["another country", storedDocument({ country: "uk" })],
  ["a repo that is not owner/name", storedDocument({ hfRepo: "populace-us" })],
  ["a commit that is not one", storedDocument({ hfCommitSha: "main" })],
  ["a time that is not one", storedDocument({ copiedAt: "yesterday" })],
  ["an unknown field", storedDocument({ extra: true })],
  ["a record that is not an object", storedDocument({ record: "text" })],
  ["a byte count that differs", storedDocument({
    record: { ...(storedDocument().record as object), bytes: 1 },
  })],
  ["a digest that differs", storedDocument({
    record: { ...(storedDocument().record as object), sha256: "0".repeat(64) },
  })],
  ["a record the rules refuse", (() => {
    const bytes = recordBytes("us-calibration-attempt-unlisted");
    return storedDocument({ record: { sha256: sha256(bytes), bytes: bytes.byteLength, text: decoded(bytes) } });
  })()],
];

test.each(BAD_DOCUMENTS)("a stored copy with %s makes the reader throw", async (_case, document) => {
  const { readStoredReleasesRecord } = await readerModule();
  const store = new InMemoryBlobStore();
  store.storeDocument(document);

  await expect(readStoredReleasesRecord({ country: "us", token: TOKEN, client: store.client() }))
    .rejects.toBeInstanceOf(Error);
});

test("a store answer other than 200 makes the reader throw", async () => {
  const { readStoredReleasesRecord } = await readerModule();
  const store = new InMemoryBlobStore();
  store.storeDocument();
  store.answeredStatus = 304;

  await expect(readStoredReleasesRecord({ country: "us", token: TOKEN, client: store.client() }))
    .rejects.toThrow("304");
});

// Writing the copy ---------------------------------------------------------------

test("the first copy is created only if there still is none, with the file's exact text", async () => {
  const store = new InMemoryBlobStore();

  const written = await writeCopy(store);

  expect(store.puts).toHaveLength(1);
  expect(store.puts[0]!.pathname).toBe(STORED_PATH);
  expect(store.puts[0]!.options).toEqual({
    access: "private",
    token: TOKEN,
    addRandomSuffix: false,
    allowOverwrite: false,
    contentType: "application/json; charset=utf-8",
    cacheControlMaxAge: 60,
  });
  expect(store.document()).toEqual(storedDocument());
  expect(written).toEqual({
    country: "us",
    hfCommitSha: COMMIT,
    hasRecord: true,
    sha256: sha256(recordBytes()),
    etag: store.blobs.get(STORED_PATH)!.etag,
  });
});

test("a later copy replaces only the version its execution read", async () => {
  const store = new InMemoryBlobStore();
  const readEtag = store.storeDocument();
  const bytes = recordBytes(null);

  await writeCopy(store, {
    hfCommitSha: NEWER_COMMIT,
    expectedEtag: readEtag,
    recordFile: temporaryFile("releases.json", bytes),
  });

  expect(store.puts[0]!.options).toMatchObject({ allowOverwrite: true, ifMatch: readEtag });
  expect(store.document().hfCommitSha).toBe(NEWER_COMMIT);
  expect(store.document().record.text).toBe(decoded(bytes));
});

test("a repo without releases.json gets a copy that says so", async () => {
  const { readStoredReleasesRecord } = await readerModule();
  const store = new InMemoryBlobStore();

  const written = await writeCopy(store, { recordFile: null });

  expect(store.document().record).toBeNull();
  expect(written).toMatchObject({ hasRecord: false, sha256: null });
  await expect(readStoredReleasesRecord({ country: "us", token: TOKEN, client: store.client() }))
    .resolves.toMatchObject({ state: "no_record", hfCommitSha: COMMIT });
});

test("a leading byte order mark survives the copy, so the digest is of the file's bytes", async () => {
  const { readStoredReleasesRecord } = await readerModule();
  const store = new InMemoryBlobStore();
  const bytes = new Uint8Array([0xef, 0xbb, 0xbf, ...recordBytes()]);

  await writeCopy(store, { recordFile: temporaryFile("releases.json", bytes) });

  expect(store.document().record.bytes).toBe(bytes.byteLength);
  await expect(readStoredReleasesRecord({ country: "us", token: TOKEN, client: store.client() }))
    .resolves.toMatchObject({ state: "record", sha256: sha256(bytes) });
});

test("a copy another execution replaced after it was read is not overwritten", async () => {
  const command = await copyCommand();
  const store = new InMemoryBlobStore();
  const readEtag = store.storeDocument();
  const newer = storedDocument({ hfCommitSha: NEWER_COMMIT });
  store.beforeWrite = () => store.storeDocument(newer);

  const error = await writeCopy(store, { expectedEtag: readEtag }).catch((caught: unknown) => caught);

  expect(error).toBeInstanceOf(command.ReleasesRecordCopyConflictError);
  expect(command.releasesRecordCopyExitCode(error)).toBe(4);
  expect(store.document()).toEqual(newer);
});

test("a first copy another execution created meanwhile is not overwritten", async () => {
  const command = await copyCommand();
  const store = new InMemoryBlobStore();
  const newer = storedDocument({ hfCommitSha: NEWER_COMMIT });
  store.beforeWrite = () => store.storeDocument(newer);

  const error = await writeCopy(store).catch((caught: unknown) => caught);

  expect(error).toBeInstanceOf(command.ReleasesRecordCopyConflictError);
  expect(command.releasesRecordCopyExitCode(error)).toBe(4);
  expect(store.document()).toEqual(newer);
});

test("a failed first write with nothing stored is a store failure, not a conflict", async () => {
  const command = await copyCommand();
  const store = new InMemoryBlobStore();
  store.writeError = new BlobError("The store is unavailable.");

  const error = await writeCopy(store).catch((caught: unknown) => caught);

  expect(error).not.toBeInstanceOf(command.ReleasesRecordCopyConflictError);
  expect(command.releasesRecordCopyExitCode(error)).toBe(3);
});

test("a write the store did not keep fails verification", async () => {
  const command = await copyCommand();
  const store = new InMemoryBlobStore();
  const readEtag = store.storeDocument();
  store.ignoresWrites = true;

  const error = await writeCopy(store, { expectedEtag: readEtag }).catch((caught: unknown) => caught);

  expect(error).toBeInstanceOf(Error);
  expect(command.releasesRecordCopyExitCode(error)).toBe(3);
});

test("a record the package's rules refuse is not written", async () => {
  const command = await copyCommand();
  const store = new InMemoryBlobStore();
  const refused = recordBytes("us-calibration-attempt-unlisted");

  const error = await writeCopy(store, { recordFile: temporaryFile("releases.json", refused) })
    .catch((caught: unknown) => caught);

  expect(command.releasesRecordCopyExitCode(error)).toBe(2);
  expect(store.puts).toEqual([]);
});

test("a record file that cannot be read is a request error", async () => {
  const command = await copyCommand();
  const store = new InMemoryBlobStore();

  const error = await writeCopy(store, { recordFile: join(tmpdir(), "no-such-releases-record.json") })
    .catch((caught: unknown) => caught);

  expect(command.releasesRecordCopyExitCode(error)).toBe(1);
  expect(store.puts).toEqual([]);
});

const REFUSED_REQUESTS: Array<[string, unknown]> = [
  ["not an object", ["us"]],
  ["an unknown field", { ...copyRequestFields(), extra: true }],
  ["a country not registered", { ...copyRequestFields(), country: "fr" }],
  ["a repo that is not owner/name", { ...copyRequestFields(), hfRepo: "populace-us" }],
  ["a commit that is not one", { ...copyRequestFields(), hfCommitSha: "main" }],
  ["an empty ETag", { ...copyRequestFields(), expectedEtag: "" }],
  ["a weak ETag", { ...copyRequestFields(), expectedEtag: 'W/"etag-1"' }],
  ["a record file that is not a path", { ...copyRequestFields(), recordFile: 1 }],
  ["no record file field", { country: "us", hfRepo: REPO, hfCommitSha: COMMIT, expectedEtag: null }],
];

function copyRequestFields(): Record<string, unknown> {
  return { country: "us", hfRepo: REPO, hfCommitSha: COMMIT, expectedEtag: null, recordFile: null };
}

test.each(REFUSED_REQUESTS)("a request with %s is refused", async (_case, request) => {
  const command = await copyCommand();

  expect(() => command.parseReleasesRecordCopyRequest(request))
    .toThrow(command.ReleasesRecordCopyRequestError);
});

// Reading the stored version -----------------------------------------------------

test("with no copy stored, the version has no ETag or commit", async () => {
  const command = await copyCommand();
  const store = new InMemoryBlobStore();

  await expect(command.readReleasesRecordCopyVersion({ country: "us", token: TOKEN, client: store.client() }))
    .resolves.toEqual({ country: "us", etag: null, hfCommitSha: null, hasRecord: null });
});

test("the version is read past the cache and uncompressed, so its ETag is the strong one", async () => {
  const command = await copyCommand();
  const store = new InMemoryBlobStore();
  const etag = store.storeDocument();
  store.compressesReads = true;

  await expect(command.readReleasesRecordCopyVersion({ country: "us", token: TOKEN, client: store.client() }))
    .resolves.toEqual({ country: "us", etag, hfCommitSha: COMMIT, hasRecord: true });
  expect(store.gets).toEqual([
    { pathname: STORED_PATH, access: "private", useCache: false, acceptEncoding: "identity", token: TOKEN },
  ]);
});

test("a stored copy that breaks the rules can still be replaced", async () => {
  const command = await copyCommand();
  const store = new InMemoryBlobStore();
  const etag = store.storeDocument("not json");

  await expect(command.readReleasesRecordCopyVersion({ country: "us", token: TOKEN, client: store.client() }))
    .resolves.toEqual({ country: "us", etag, hfCommitSha: null, hasRecord: null });
});

test.each([["no", ""], ["a weak", 'W/"etag-1"']])("a copy read with %s ETag is a store failure", async (_case, etag) => {
  const command = await copyCommand();
  const store = new InMemoryBlobStore();
  store.storeDocument();
  store.answeredEtag = etag;

  const error = await command.readReleasesRecordCopyVersion({ country: "us", token: TOKEN, client: store.client() })
    .catch((caught: unknown) => caught);

  expect(error).toBeInstanceOf(Error);
  expect(command.releasesRecordCopyExitCode(error)).toBe(3);
});

test("a weak ETag's error names the stored copy, not the tree manifest", async () => {
  const command = await copyCommand();
  const store = new InMemoryBlobStore();
  store.storeDocument();
  store.answeredEtag = 'W/"etag-1"';

  const error = await command.readReleasesRecordCopyVersion({ country: "us", token: TOKEN, client: store.client() })
    .catch((caught: unknown) => caught);

  expect((error as Error).message).toContain(STORED_PATH);
  expect((error as Error).message).not.toContain("manifest");
});

// The command line ---------------------------------------------------------------

async function printedBy(run: () => Promise<number>): Promise<{ exitCode: number; printed: unknown[] }> {
  const log = spyOn(console, "log").mockImplementation(() => {});
  const failure = spyOn(console, "error").mockImplementation(() => {});
  try {
    const exitCode = await run();
    return { exitCode, printed: log.mock.calls.map((call) => JSON.parse(String(call[0]))) };
  } finally {
    log.mockRestore();
    failure.mockRestore();
  }
}

test("read prints the stored version as one JSON line", async () => {
  const command = await copyCommand();
  const store = new InMemoryBlobStore();
  const etag = store.storeDocument();

  const { exitCode, printed } = await printedBy(() =>
    command.releasesRecordCopyMain(["read", "us"], WITH_TOKEN, store.client()));

  expect(exitCode).toBe(0);
  expect(printed).toEqual([{ country: "us", etag, hfCommitSha: COMMIT, hasRecord: true }]);
});

test("write prints what it stored as one JSON line", async () => {
  const command = await copyCommand();
  const store = new InMemoryBlobStore();
  const requestFile = temporaryFile("copy.json", JSON.stringify(copyRequest()));

  const { exitCode, printed } = await printedBy(() =>
    command.releasesRecordCopyMain(["write", requestFile], WITH_TOKEN, store.client(), NOW));

  expect(exitCode).toBe(0);
  expect(printed).toEqual([{
    country: "us",
    hfCommitSha: COMMIT,
    hasRecord: true,
    sha256: sha256(recordBytes()),
    etag: store.blobs.get(STORED_PATH)!.etag,
  }]);
});

test("write exits 4 when another execution changed the copy", async () => {
  const command = await copyCommand();
  const store = new InMemoryBlobStore();
  store.storeDocument();
  const requestFile = temporaryFile("copy.json", JSON.stringify(copyRequest({ expectedEtag: '"etag-old"' })));

  const { exitCode, printed } = await printedBy(() =>
    command.releasesRecordCopyMain(["write", requestFile], WITH_TOKEN, store.client(), NOW));

  expect(exitCode).toBe(4);
  expect(printed).toEqual([]);
});

test.each([
  ["no token", ["read", "us"], {}],
  ["no arguments", [], WITH_TOKEN],
  ["another action", ["delete", "us"], WITH_TOKEN],
  ["a country not registered", ["read", "fr"], WITH_TOKEN],
  ["a request that is not JSON", ["write", "/no/such/copy.json"], WITH_TOKEN],
])("the command exits 1 with %s", async (_case, argv, environment) => {
  const command = await copyCommand();
  const store = new InMemoryBlobStore();

  const { exitCode } = await printedBy(() =>
    command.releasesRecordCopyMain(argv as string[], environment as Record<string, string>, store.client()));

  expect(exitCode).toBe(1);
  expect(store.gets).toEqual([]);
  expect(store.puts).toEqual([]);
});
