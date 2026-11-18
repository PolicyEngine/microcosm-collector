// Publishing a tree the worker wrote to disk: its parts are uploaded to their
// immutable paths and its entry is added to the manifest with a
// compare-and-swap, against an in-memory store that follows Vercel Blob's
// rules for allowOverwrite and ifMatch.
import { afterEach, expect, spyOn, test } from "bun:test";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { gunzipSync } from "node:zlib";
import { BlobError, BlobPreconditionFailedError } from "@vercel/blob";

import type { CalibrationTreeBlobClient } from "./calibration-tree-blob";
import { CALIBRATION_TREE_MANIFEST_PATH, parseCalibrationTreeManifest } from "./calibration-tree-manifest";
import * as publication from "./calibration-tree-publication";
import { microcosmRepo } from "./latest-artifact";
import { goldenReleaseInputs } from "./test-support/calibration-tree-golden-cases";

const WORKER = join(import.meta.dir, "calibration-tree-worker.ts");
const PUBLICATION = join(import.meta.dir, "calibration-tree-publication.ts");
const TOKEN = "vercel_blob_rw_test_token";
const RUN_ID = "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71";

const temporaryDirectories: string[] = [];

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function temporaryDirectory(): string {
  const directory = mkdtempSync(join(tmpdir(), "calibration-tree-publication-"));
  temporaryDirectories.push(directory);
  return directory;
}

interface StoredBlob {
  bytes: Uint8Array;
  etag: string;
}

interface RecordedPut {
  pathname: string;
  allowOverwrite: boolean | undefined;
  ifMatch: string | undefined;
}

// Vercel Blob's write rules: without allowOverwrite an existing blob is not
// replaced, and with a non-empty ifMatch a blob whose ETag differs is not
// replaced.
class InMemoryBlobStore {
  readonly blobs = new Map<string, StoredBlob>();
  readonly puts: RecordedPut[] = [];
  readonly putOptions = new Map<string, Record<string, unknown>>();
  // Paths whose writes fail, and paths whose writes store other bytes.
  readonly failingPuts = new Set<string>();
  readonly corruptedPuts = new Set<string>();
  private written = 0;
  beforeManifestWrite: (() => void) | null = null;
  // Set to answer the manifest's reads with this ETag instead of its own.
  answeredManifestEtag: string | null = null;
  // Set to answer reads as Vercel Blob does: compressed, with the weak form
  // of the ETag (W/"…"), unless the read asks for no content encoding.
  compressesReads = false;

  store(pathname: string, bytes: Uint8Array): void {
    this.written += 1;
    this.blobs.set(pathname, { bytes, etag: `"etag-${this.written}"` });
  }

  client(): CalibrationTreeBlobClient {
    const get = async (pathname: string, options: { headers?: Record<string, string> } = {}) => {
      const stored = this.blobs.get(pathname);
      if (!stored) return null;
      const etag = pathname === CALIBRATION_TREE_MANIFEST_PATH && this.answeredManifestEtag !== null
        ? this.answeredManifestEtag
        : stored.etag;
      const compressed = this.compressesReads && options.headers?.["accept-encoding"] !== "identity";
      return {
        statusCode: 200,
        stream: new Response(Buffer.from(stored.bytes)).body,
        headers: new Headers(compressed ? { "content-encoding": "br" } : {}),
        blob: {
          pathname,
          etag: compressed ? `W/${etag}` : etag,
          contentType: "application/octet-stream",
          size: stored.bytes.byteLength,
        },
      };
    };
    const put = async (
      pathname: string,
      body: string | Buffer,
      options: { allowOverwrite?: boolean; ifMatch?: string } & Record<string, unknown>,
    ) => {
      this.puts.push({ pathname, allowOverwrite: options.allowOverwrite, ifMatch: options.ifMatch });
      this.putOptions.set(pathname, options);
      if (this.failingPuts.has(pathname)) throw new Error("The store refused the write.");
      if (this.corruptedPuts.has(pathname)) {
        this.store(pathname, Bun.gzipSync(new TextEncoder().encode("other bytes")));
        return { pathname };
      }
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

  manifest() {
    const stored = this.blobs.get(CALIBRATION_TREE_MANIFEST_PATH);
    return stored ? parseCalibrationTreeManifest(JSON.parse(new TextDecoder().decode(stored.bytes))) : null;
  }
}

interface BuiltSourceFile {
  file: string;
  path: string;
  sha256: string;
  rawBytes: number;
}

interface BuiltTree {
  bundleDirectory: string;
  worker: {
    buildArtifactId: string;
    files: Array<{ part: string; path: string; sha256: string; rawBytes: number }>;
    manifestEntry: Record<string, unknown>;
    sourceFiles?: BuiltSourceFile[];
  };
}

// The worker's own output for a recorded release's diagnostics, as a staged
// bundle or as a build folder of the release.
function builtTree(
  kind: "staging" | "release",
  diagnosticsText?: string,
  options: { build?: string; createdAt?: string; extraFiles?: Record<string, { name: string; text: string }> } = {},
): BuiltTree {
  const release = goldenReleaseInputs()["uk-release"];
  const build = options.build ?? RUN_ID;
  const inputDirectory = temporaryDirectory();
  writeFileSync(join(inputDirectory, "calibration_diagnostics.json"), diagnosticsText ?? release.diagnosticsText);
  const folder = kind === "staging" ? `staged/${RUN_ID}` : `releases/${release.releaseId}/${build}`;
  const extraArtifacts = Object.fromEntries(
    Object.entries(options.extraFiles ?? {}).map(([artifact, { name, text }]) => {
      writeFileSync(join(inputDirectory, name), text);
      return [artifact, { path: `${folder}/${name}`, file: join(inputDirectory, name) }];
    }),
  );
  const shared = {
    country: "uk",
    hfRepo: microcosmRepo("uk"),
    hfCommitSha: release.hfCommitSha,
    createdAt: options.createdAt ?? "2026-10-19T09:41:07.000Z",
    artifacts: {
      calibrationDiagnostics: {
        path: `${folder}/calibration_diagnostics.json`,
        file: join(inputDirectory, "calibration_diagnostics.json"),
      },
      buildManifest: null,
      releaseManifest: null,
      demographics: null,
      ...extraArtifacts,
    },
  };
  const request = kind === "staging"
    ? { kind, runId: RUN_ID, label: release.releaseId, ...shared }
    : {
        kind,
        releaseId: `${release.releaseId}/${build}`,
        releaseDirectory: `${release.releaseId}/${build}`,
        ...shared,
      };
  const requestPath = join(inputDirectory, "request.json");
  writeFileSync(requestPath, JSON.stringify(request));
  const bundleDirectory = join(temporaryDirectory(), "trees");
  const run = Bun.spawnSync({
    cmd: [process.execPath, "run", WORKER, requestPath, bundleDirectory],
    env: { PATH: process.env.PATH ?? "" },
    stdout: "pipe",
    stderr: "pipe",
  });
  expect({ exitCode: run.exitCode, stderr: run.stderr.toString() }).toEqual({ exitCode: 0, stderr: "" });
  return { bundleDirectory, worker: JSON.parse(run.stdout.toString()) };
}

function publish(store: InMemoryBlobStore, tree: BuiltTree, makeLatest = false) {
  return publication.publishBuiltCalibrationTree({
    request: publication.parseCalibrationTreePublicationRequest({
      bundleDirectory: tree.bundleDirectory,
      worker: tree.worker,
      makeLatest,
    }),
    token: TOKEN,
    client: store.client(),
  });
}

test("a built tree's parts are uploaded from disk and the manifest is created with its entry", async () => {
  const store = new InMemoryBlobStore();
  const tree = builtTree("staging");

  const published = await publish(store, tree);

  for (const file of tree.worker.files) {
    const stored = store.blobs.get(file.path);
    expect(stored?.bytes).toEqual(new Uint8Array(readFileSync(join(tree.bundleDirectory, file.path))));
  }
  expect(store.manifest()?.countries.uk?.builds as unknown).toEqual([tree.worker.manifestEntry]);
  // An absent manifest is created only if it is still absent.
  expect(store.puts.filter((put) => put.pathname === CALIBRATION_TREE_MANIFEST_PATH)).toEqual([
    { pathname: CALIBRATION_TREE_MANIFEST_PATH, allowOverwrite: false, ifMatch: undefined },
  ]);
  // The index goes last, after every part it describes.
  expect(store.puts.filter((put) => put.pathname !== CALIBRATION_TREE_MANIFEST_PATH).at(-1)?.pathname)
    .toBe(tree.worker.files.find((file) => file.part === "index")?.path);
  expect(published).toEqual({
    buildArtifactId: tree.worker.buildArtifactId,
    kind: "staging",
    uploadedParts: tree.worker.files.length,
    keptParts: 0,
    uploadedSourceFiles: 1,
    keptSourceFiles: 0,
    sourceFileConflicts: [],
    madeLatest: false,
  });
});

test("an existing manifest is replaced only if its ETag is still the one read", async () => {
  const store = new InMemoryBlobStore();
  await publish(store, builtTree("staging"));
  const etagBefore = store.blobs.get(CALIBRATION_TREE_MANIFEST_PATH)?.etag;
  const release = builtTree("release");

  await publish(store, release);

  expect(store.puts.filter((put) => put.pathname === CALIBRATION_TREE_MANIFEST_PATH).at(-1)).toEqual({
    pathname: CALIBRATION_TREE_MANIFEST_PATH,
    allowOverwrite: true,
    ifMatch: etagBefore,
  });
  // The manifest orders builds by time, then build ID.
  expect(store.manifest()?.countries.uk?.builds.map((build) => build.kind).sort()).toEqual(["release", "staging"]);
});

test("a manifest another writer changed is never overwritten: the publication fails so it can run again", async () => {
  const store = new InMemoryBlobStore();
  await publish(store, builtTree("staging"));
  const release = builtTree("release");
  const otherWriterManifest = new TextEncoder().encode(JSON.stringify({ schemaVersion: 6, countries: {} }));
  store.beforeManifestWrite = () => {
    store.beforeManifestWrite = null;
    store.store(CALIBRATION_TREE_MANIFEST_PATH, otherWriterManifest);
  };

  await expect(publish(store, release)).rejects.toBeInstanceOf(publication.CalibrationTreeManifestConflictError);

  expect(store.blobs.get(CALIBRATION_TREE_MANIFEST_PATH)?.bytes).toEqual(otherWriterManifest);
  expect(publication.calibrationTreePublicationExitCode(new publication.CalibrationTreeManifestConflictError("x")))
    .toBe(publication.CALIBRATION_TREE_PUBLICATION_EXIT_PUBLICATION_ERROR);
});

test("a part already stored with the same bytes is kept, and publishing again changes nothing", async () => {
  const store = new InMemoryBlobStore();
  const tree = builtTree("staging");
  await publish(store, tree);
  const manifestBefore = store.manifest();
  const partPutsBefore = store.puts.filter((put) => put.pathname !== CALIBRATION_TREE_MANIFEST_PATH).length;

  const published = await publish(store, tree);

  expect(store.puts.filter((put) => put.pathname !== CALIBRATION_TREE_MANIFEST_PATH).length).toBe(partPutsBefore);
  expect(store.manifest()).toEqual(manifestBefore);
  expect(published).toMatchObject({ uploadedParts: 0, keptParts: tree.worker.files.length });
});

test("a stored part with other bytes stops the publication before the manifest changes", async () => {
  const store = new InMemoryBlobStore();
  const tree = builtTree("staging");
  const [firstPart] = tree.worker.files;
  store.store(firstPart.path, Bun.gzipSync(new TextEncoder().encode("{}")));

  await expect(publish(store, tree)).rejects.toThrow(/already exists with different content/);

  expect(store.manifest()).toBeNull();
});

test("a part on disk that differs from the worker's record stops the publication before anything is uploaded", async () => {
  const store = new InMemoryBlobStore();
  const tree = builtTree("staging");
  const [firstPart] = tree.worker.files;
  const raw = gunzipSync(readFileSync(join(tree.bundleDirectory, firstPart.path)));
  writeFileSync(join(tree.bundleDirectory, firstPart.path), Bun.gzipSync(new Uint8Array([...raw, 0x20])));

  await expect(publish(store, tree)).rejects.toBeInstanceOf(publication.CalibrationTreePublicationRequestError);

  expect(store.puts).toEqual([]);
});

test("another build already recorded for the same run is refused, not retried", async () => {
  const store = new InMemoryBlobStore();
  await publish(store, builtTree("staging"));
  const release = goldenReleaseInputs()["uk-release"];
  const otherFiles = builtTree("staging", `${release.diagnosticsText}\n`);
  const manifestBefore = store.manifest();

  const refusal = await publish(store, otherFiles).catch((error: unknown) => error);

  expect(refusal).toBeInstanceOf(publication.CalibrationTreeEntryRefusedError);
  expect(publication.calibrationTreePublicationExitCode(refusal))
    .toBe(publication.CALIBRATION_TREE_PUBLICATION_EXIT_ENTRY_REFUSED);
  expect(store.manifest()).toEqual(manifestBefore);
});

test("a release build becomes the country's latest only when asked to", async () => {
  const store = new InMemoryBlobStore();
  const release = builtTree("release");

  await publish(store, release, false);
  expect(store.manifest()?.countries.uk?.latestReleaseBuildArtifactId).toBeNull();

  const published = await publish(store, release, true);

  expect(store.manifest()?.countries.uk?.latestReleaseBuildArtifactId).toBe(release.worker.buildArtifactId);
  expect(published.madeLatest).toBe(true);
});

test("a staging build can never become the latest release", async () => {
  const store = new InMemoryBlobStore();

  const refusal = await publish(store, builtTree("staging"), true).catch((error: unknown) => error);

  expect(refusal).toBeInstanceOf(publication.CalibrationTreeEntryRefusedError);
  expect(store.manifest()).toBeNull();
});

test("an older build of the default release never takes the latest from a newer one", async () => {
  const store = new InMemoryBlobStore();
  const newer = builtTree("release", undefined, {
    build: "uk-frs-calibration-attempt-20261101T000000Z-0a1b2c3d",
    createdAt: "2026-11-01T00:00:00.000Z",
  });
  const older = builtTree("release", undefined, { createdAt: "2026-10-19T09:41:07.000Z" });
  await publish(store, newer, true);

  // A retried or reprocessed older build publishes after the newer one.
  const published = await publish(store, older, true);

  const uk = store.manifest()?.countries.uk;
  expect(uk?.latestReleaseBuildArtifactId).toBe(newer.worker.buildArtifactId);
  expect(uk?.builds.map((build) => build.buildArtifactId).sort())
    .toEqual([newer.worker.buildArtifactId, older.worker.buildArtifactId].sort());
  expect(published.madeLatest).toBe(false);
});

test("a second submission of the same files for the same run is refused, not retried", async () => {
  const store = new InMemoryBlobStore();
  await publish(store, builtTree("staging"));
  const manifestBefore = store.manifest();
  // The same files give the same build ID, but a later accepted_at gives the
  // index other content.
  const resubmitted = builtTree("staging", undefined, { createdAt: "2026-11-02T08:00:00.000Z" });

  const refusal = await publish(store, resubmitted).catch((error: unknown) => error);

  expect(refusal).toBeInstanceOf(publication.CalibrationTreeEntryRefusedError);
  expect(publication.calibrationTreePublicationExitCode(refusal))
    .toBe(publication.CALIBRATION_TREE_PUBLICATION_EXIT_ENTRY_REFUSED);
  expect(store.manifest()).toEqual(manifestBefore);
});

test("a second publication succeeds when the store compresses reads and weakens their ETags, as Vercel Blob does", async () => {
  const store = new InMemoryBlobStore();
  store.compressesReads = true;
  await publish(store, builtTree("staging"));
  const etagBefore = store.blobs.get(CALIBRATION_TREE_MANIFEST_PATH)?.etag;

  await expect(publish(store, builtTree("release"))).resolves.toMatchObject({ kind: "release" });

  // The write is conditional on the stored manifest's own, strong ETag.
  expect(store.puts.filter((put) => put.pathname === CALIBRATION_TREE_MANIFEST_PATH).at(-1)).toEqual({
    pathname: CALIBRATION_TREE_MANIFEST_PATH,
    allowOverwrite: true,
    ifMatch: etagBefore,
  });
  expect(store.manifest()?.countries.uk?.builds.map((build) => build.kind).sort()).toEqual(["release", "staging"]);
});

test("a manifest another writer changed is still never overwritten when the store compresses reads", async () => {
  const store = new InMemoryBlobStore();
  store.compressesReads = true;
  await publish(store, builtTree("staging"));
  const release = builtTree("release");
  const otherWriterManifest = new TextEncoder().encode(JSON.stringify({ schemaVersion: 6, countries: {} }));
  store.beforeManifestWrite = () => {
    store.beforeManifestWrite = null;
    store.store(CALIBRATION_TREE_MANIFEST_PATH, otherWriterManifest);
  };

  await expect(publish(store, release)).rejects.toBeInstanceOf(publication.CalibrationTreeManifestConflictError);

  expect(store.blobs.get(CALIBRATION_TREE_MANIFEST_PATH)?.bytes).toEqual(otherWriterManifest);
});

test("a manifest read with a weak ETag is never sent as the write's condition", async () => {
  const store = new InMemoryBlobStore();
  await publish(store, builtTree("staging"));
  const manifestBefore = store.blobs.get(CALIBRATION_TREE_MANIFEST_PATH);
  const manifestWrites = () => store.puts.filter((put) => put.pathname === CALIBRATION_TREE_MANIFEST_PATH).length;
  const manifestWritesBefore = manifestWrites();
  // A store that answers even an uncompressed read with a weak ETag.
  store.answeredManifestEtag = `W/${manifestBefore?.etag}`;

  const failure = await publish(store, builtTree("release")).catch((error: unknown) => error);

  expect(failure).toBeInstanceOf(publication.CalibrationTreeManifestConflictError);
  expect((failure as Error).message).toMatch(/weak ETag/);
  expect(manifestWrites()).toBe(manifestWritesBefore);
  expect(store.blobs.get(CALIBRATION_TREE_MANIFEST_PATH)?.bytes).toEqual(manifestBefore?.bytes);
});

test("a manifest read without an ETag is never written over", async () => {
  const store = new InMemoryBlobStore();
  await publish(store, builtTree("staging"));
  const manifestBefore = store.blobs.get(CALIBRATION_TREE_MANIFEST_PATH)?.bytes;
  store.answeredManifestEtag = "";

  await expect(publish(store, builtTree("release"))).rejects.toBeInstanceOf(
    publication.CalibrationTreeManifestConflictError,
  );

  expect(store.blobs.get(CALIBRATION_TREE_MANIFEST_PATH)?.bytes).toEqual(manifestBefore);
});

const EXTRA_FILES = {
  parentBuildManifest: { name: "parent_build_manifest.json", text: '{"build_id":"the-parent-build"}' },
  reformValidation: { name: "reform_validation.json", text: '{"schema_version":1,"reforms":[]}' },
};

function sourceFilesOf(tree: BuiltTree): BuiltSourceFile[] {
  expect(tree.worker.sourceFiles?.length ?? 0).toBeGreaterThan(0);
  return tree.worker.sourceFiles!;
}

test("a build's original files are stored privately before any part of its tree", async () => {
  const store = new InMemoryBlobStore();
  const tree = builtTree("release", undefined, { extraFiles: EXTRA_FILES });
  const sourceFiles = sourceFilesOf(tree);
  expect(sourceFiles.map((file) => file.file)).toEqual([
    "calibrationDiagnostics",
    "parentBuildManifest",
    "reformValidation",
  ]);

  const published = await publish(store, tree);

  const written = store.puts.map((put) => put.pathname);
  expect(written.slice(0, sourceFiles.length)).toEqual(sourceFiles.map((file) => file.path));
  expect(written.at(-1)).toBe(CALIBRATION_TREE_MANIFEST_PATH);
  for (const file of sourceFiles) {
    expect(store.blobs.get(file.path)?.bytes)
      .toEqual(new Uint8Array(readFileSync(join(tree.bundleDirectory, file.path))));
    expect(store.putOptions.get(file.path)).toMatchObject({
      access: "private",
      addRandomSuffix: false,
      contentType: "application/gzip",
    });
    expect(store.putOptions.get(file.path)?.allowOverwrite).toBeUndefined();
  }
  expect(published).toMatchObject({ uploadedSourceFiles: 3, keptSourceFiles: 0 });
});

test("an original file that cannot be stored stops the publication before any part", async () => {
  const store = new InMemoryBlobStore();
  const tree = builtTree("staging");
  const [diagnostics] = sourceFilesOf(tree);
  store.failingPuts.add(diagnostics.path);

  await expect(publish(store, tree)).rejects.toThrow("The store refused the write.");

  expect(store.puts.map((put) => put.pathname)).toEqual([diagnostics.path]);
  expect(store.manifest()).toBeNull();
});

test("after a failed part, no entry is recorded and publishing again keeps the copies and finishes", async () => {
  const store = new InMemoryBlobStore();
  const tree = builtTree("staging");
  const [firstPart] = tree.worker.files;
  sourceFilesOf(tree);
  store.failingPuts.add(firstPart.path);

  await expect(publish(store, tree)).rejects.toThrow("The store refused the write.");
  expect(store.manifest()).toBeNull();
  store.failingPuts.clear();

  const published = await publish(store, tree);

  expect(published).toMatchObject({ uploadedSourceFiles: 0, keptSourceFiles: 1 });
  expect(store.manifest()?.countries.uk?.builds as unknown).toEqual([tree.worker.manifestEntry]);
});

test("a stored copy with other bytes is kept and named, and the rest is published", async () => {
  const store = new InMemoryBlobStore();
  const tree = builtTree("release", undefined, { extraFiles: EXTRA_FILES });
  const [diagnostics, ...others] = sourceFilesOf(tree);
  const storedCopy = Bun.gzipSync(new TextEncoder().encode("{}"));
  store.store(diagnostics.path, storedCopy);

  const published = await publish(store, tree);

  expect(published).toMatchObject({
    uploadedSourceFiles: others.length,
    keptSourceFiles: 0,
    sourceFileConflicts: ["calibrationDiagnostics"],
  });
  expect(store.blobs.get(diagnostics.path)?.bytes).toEqual(storedCopy);
  for (const file of others) expect(store.blobs.has(file.path)).toBe(true);
  expect(store.manifest()?.countries.uk?.builds as unknown).toEqual([tree.worker.manifestEntry]);
});

test("a refused build still names the copies that differed", async () => {
  expect(typeof publication.calibrationTreePublicationRefusalAnswer).toBe("function");
  const store = new InMemoryBlobStore();
  await publish(store, builtTree("staging"));
  const resubmitted = builtTree("staging", undefined, { createdAt: "2026-11-02T08:00:00.000Z" });
  const [diagnostics] = sourceFilesOf(resubmitted);
  store.store(diagnostics.path, Bun.gzipSync(new TextEncoder().encode("{}")));

  const refusal = await publish(store, resubmitted).catch((error: unknown) => error);

  expect(refusal).toBeInstanceOf(publication.CalibrationTreeEntryRefusedError);
  expect(publication.calibrationTreePublicationRefusalAnswer(refusal))
    .toBe(JSON.stringify({ sourceFileConflicts: ["calibrationDiagnostics"] }));
  expect(publication.calibrationTreePublicationRefusalAnswer(new publication.CalibrationTreeEntryRefusedError("x")))
    .toBeNull();
  expect(publication.calibrationTreePublicationRefusalAnswer(new Error("x"))).toBeNull();
});

test("the command prints a refused build's differing copies before it exits", async () => {
  const store = new InMemoryBlobStore();
  await publish(store, builtTree("staging"));
  const resubmitted = builtTree("staging", undefined, { createdAt: "2026-11-02T08:00:00.000Z" });
  const [diagnostics] = sourceFilesOf(resubmitted);
  store.store(diagnostics.path, Bun.gzipSync(new TextEncoder().encode("{}")));
  const requestPath = join(temporaryDirectory(), "publication.json");
  writeFileSync(requestPath, JSON.stringify({
    bundleDirectory: resubmitted.bundleDirectory,
    worker: resubmitted.worker,
    makeLatest: false,
  }));
  const printed = spyOn(console, "log").mockImplementation(() => {});
  const reported = spyOn(console, "error").mockImplementation(() => {});

  try {
    const exitCode = await publication.calibrationTreePublicationMain(
      [requestPath],
      { CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN: TOKEN },
      store.client(),
    );

    expect(exitCode).toBe(publication.CALIBRATION_TREE_PUBLICATION_EXIT_ENTRY_REFUSED);
    expect(printed.mock.calls).toEqual([[JSON.stringify({ sourceFileConflicts: ["calibrationDiagnostics"] })]]);
  } finally {
    printed.mockRestore();
    reported.mockRestore();
  }
});

test("a copy that does not read back as written stops the publication", async () => {
  const store = new InMemoryBlobStore();
  const tree = builtTree("staging");
  const [diagnostics] = sourceFilesOf(tree);
  store.corruptedPuts.add(diagnostics.path);

  await expect(publish(store, tree)).rejects.toThrow(/failed verification/);

  expect(store.manifest()).toBeNull();
});

test("a resubmission of a stored build still stores a copy the build lacks", async () => {
  const store = new InMemoryBlobStore();
  await publish(store, builtTree("staging"));
  const resubmitted = builtTree("staging", undefined, { createdAt: "2026-11-02T08:00:00.000Z" });
  const [diagnostics] = sourceFilesOf(resubmitted);
  store.blobs.delete(diagnostics.path);

  const refusal = await publish(store, resubmitted).catch((error: unknown) => error);

  expect(refusal).toBeInstanceOf(publication.CalibrationTreeEntryRefusedError);
  expect(store.blobs.has(diagnostics.path)).toBe(true);
});

test("a copy on disk that differs from the worker's record stops the publication before anything is uploaded", async () => {
  const store = new InMemoryBlobStore();
  const tree = builtTree("staging");
  const [diagnostics] = sourceFilesOf(tree);
  writeFileSync(join(tree.bundleDirectory, diagnostics.path), Bun.gzipSync(new TextEncoder().encode("{}")));

  await expect(publish(store, tree)).rejects.toBeInstanceOf(publication.CalibrationTreePublicationRequestError);

  expect(store.puts).toEqual([]);
});

test("a worker's answer must describe the build's original files", () => {
  const tree = builtTree("staging");
  const [diagnostics] = sourceFilesOf(tree);
  const malformed: unknown[] = [
    undefined,
    [],
    [{ ...diagnostics, file: "unknownFile" }],
    [{ ...diagnostics, file: "buildManifest" }],
    [{ ...diagnostics, path: CALIBRATION_TREE_MANIFEST_PATH }],
    [{ ...diagnostics, sha256: "not-a-digest" }],
    [{ ...diagnostics, rawBytes: -1 }],
    [diagnostics, diagnostics],
  ];

  for (const sourceFiles of malformed) {
    expect(() =>
      publication.parseCalibrationTreePublicationRequest({
        bundleDirectory: tree.bundleDirectory,
        worker: { ...tree.worker, sourceFiles },
        makeLatest: false,
      })
    ).toThrow(publication.CalibrationTreePublicationRequestError);
  }
});

test("a malformed manifest entry is a request error, found before anything is uploaded", () => {
  const tree = builtTree("staging");
  const { label: _label, ...withoutLabel } = tree.worker.manifestEntry;

  expect(() =>
    publication.parseCalibrationTreePublicationRequest({
      bundleDirectory: tree.bundleDirectory,
      worker: { ...tree.worker, manifestEntry: withoutLabel },
      makeLatest: false,
    })
  ).toThrow(publication.CalibrationTreePublicationRequestError);
});

function runPublication(requestPath: string, environment: Record<string, string>) {
  const run = Bun.spawnSync({
    cmd: [process.execPath, "run", PUBLICATION, requestPath],
    env: { PATH: process.env.PATH ?? "", ...environment },
    stdout: "pipe",
    stderr: "pipe",
  });
  return { exitCode: run.exitCode, stderr: run.stderr.toString() };
}

test("the command refuses to run without its token or with a request it cannot follow", () => {
  const tree = builtTree("staging");
  const directory = temporaryDirectory();
  const requestPath = join(directory, "publication.json");
  writeFileSync(requestPath, JSON.stringify({ bundleDirectory: tree.bundleDirectory, worker: tree.worker, makeLatest: false }));
  const malformedPath = join(directory, "malformed.json");
  writeFileSync(malformedPath, JSON.stringify({ bundleDirectory: tree.bundleDirectory, worker: tree.worker }));

  const withoutToken = runPublication(requestPath, {});
  const malformed = runPublication(malformedPath, { CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN: TOKEN });

  expect(withoutToken.exitCode).toBe(publication.CALIBRATION_TREE_PUBLICATION_EXIT_REQUEST_ERROR);
  expect(withoutToken.stderr).toContain("CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN");
  expect(malformed.exitCode).toBe(publication.CALIBRATION_TREE_PUBLICATION_EXIT_REQUEST_ERROR);
  expect(withoutToken.stderr + malformed.stderr).not.toContain(TOKEN);
});
