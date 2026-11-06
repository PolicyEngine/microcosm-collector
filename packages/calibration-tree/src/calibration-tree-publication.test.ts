// Publishing a tree the worker wrote to disk: its parts are uploaded to their
// immutable paths and its entry is added to the manifest with a
// compare-and-swap, against an in-memory store that follows Vercel Blob's
// rules for allowOverwrite and ifMatch.
import { afterEach, expect, test } from "bun:test";
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
// replaced, and with ifMatch a blob whose ETag differs is not replaced.
class InMemoryBlobStore {
  readonly blobs = new Map<string, StoredBlob>();
  readonly puts: RecordedPut[] = [];
  private written = 0;
  beforeManifestWrite: (() => void) | null = null;

  store(pathname: string, bytes: Uint8Array): void {
    this.written += 1;
    this.blobs.set(pathname, { bytes, etag: `"etag-${this.written}"` });
  }

  client(): CalibrationTreeBlobClient {
    const get = async (pathname: string) => {
      const stored = this.blobs.get(pathname);
      if (!stored) return null;
      return {
        statusCode: 200,
        stream: new Response(stored.bytes).body,
        headers: new Headers(),
        blob: { pathname, etag: stored.etag, contentType: "application/octet-stream", size: stored.bytes.byteLength },
      };
    };
    const put = async (
      pathname: string,
      body: string | Buffer,
      options: { allowOverwrite?: boolean; ifMatch?: string },
    ) => {
      this.puts.push({ pathname, allowOverwrite: options.allowOverwrite, ifMatch: options.ifMatch });
      if (pathname === CALIBRATION_TREE_MANIFEST_PATH) this.beforeManifestWrite?.();
      const existing = this.blobs.get(pathname);
      if (existing && !options.allowOverwrite) {
        throw new BlobError("This blob already exists, use `allowOverwrite: true` if you want to overwrite it.");
      }
      if (options.ifMatch !== undefined && existing?.etag !== options.ifMatch) {
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

interface BuiltTree {
  bundleDirectory: string;
  worker: {
    buildArtifactId: string;
    files: Array<{ part: string; path: string; sha256: string; rawBytes: number }>;
    manifestEntry: Record<string, unknown>;
  };
}

// The worker's own output for a recorded release's diagnostics, as a staged
// bundle or as a build folder of the release.
function builtTree(kind: "staging" | "release", diagnosticsText?: string): BuiltTree {
  const release = goldenReleaseInputs()["uk-release"];
  const inputDirectory = temporaryDirectory();
  writeFileSync(join(inputDirectory, "calibration_diagnostics.json"), diagnosticsText ?? release.diagnosticsText);
  const folder = kind === "staging" ? `staged/${RUN_ID}` : `releases/${release.releaseId}/${RUN_ID}`;
  const shared = {
    country: "uk",
    hfRepo: microcosmRepo("uk"),
    hfCommitSha: release.hfCommitSha,
    createdAt: "2026-10-19T09:41:07.000Z",
    artifacts: {
      calibrationDiagnostics: {
        path: `${folder}/calibration_diagnostics.json`,
        file: join(inputDirectory, "calibration_diagnostics.json"),
      },
      buildManifest: null,
      releaseManifest: null,
      demographics: null,
    },
  };
  const request = kind === "staging"
    ? { kind, runId: RUN_ID, label: release.releaseId, ...shared }
    : {
        kind,
        releaseId: `${release.releaseId}/${RUN_ID}`,
        releaseDirectory: `${release.releaseId}/${RUN_ID}`,
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
  expect(store.manifest()?.countries.uk?.builds).toEqual([tree.worker.manifestEntry]);
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
  expect(store.manifest()?.countries.uk?.builds.map((build) => build.kind)).toEqual(["staging", "release"]);
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
  store.store(firstPart.path, new Uint8Array([0x1f, 0x8b, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00, 0x02, 0x03, 0x03, 0x00]));

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
