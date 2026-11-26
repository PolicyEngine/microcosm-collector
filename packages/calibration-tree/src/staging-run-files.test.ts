// The stored copies of staging run files: where they live, the command that
// stores them, and the readers, against an in-memory store that follows
// Vercel Blob's rules for allowOverwrite and ifMatch.
import { afterEach, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { gunzipSync, gzipSync } from "node:zlib";
import { BlobPreconditionFailedError } from "@vercel/blob";

import { parseStagingManifest } from "./staging-contract";
import { US_VERSION_2_RUN_ID, usVersion2ResultFiles, usVersion2RunFolder } from "./test-support/us-version-2-staging-run";

const TOKEN = "vercel_blob_rw_test_token";
const TOKEN_VARIABLE = "CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN";
const NOW = "2026-11-19T12:00:00.000Z";
const HF_REPO = "policyengine/populace-uk-staging";
const HF_COMMIT = "1".repeat(40);
const FINGERPRINT = "2".repeat(64);
const OTHER_FINGERPRINT = "3".repeat(64);
const BUILD_ID = "a".repeat(64);
const STRUCTURED_RUN = "uk-failed-v3-fixture";
const SCHEMA_1_RUN = "us-schema-1-run";
const FIXTURES = join(import.meta.dir, "fixtures", "staging-contract", "v3", "failed", "runs", STRUCTURED_RUN);

async function runFiles() {
  const loaded = await import("./staging-run-files").catch(() => null);
  expect(loaded).not.toBeNull();
  return loaded!;
}

async function command() {
  const loaded = await import("./staging-run-files-command").catch(() => null);
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
  const directory = mkdtempSync(join(tmpdir(), "staging-run-files-"));
  temporaryDirectories.push(directory);
  return directory;
}

function sha256(bytes: Uint8Array | string): string {
  return createHash("sha256").update(bytes).digest("hex");
}

class InMemoryBlobStore {
  readonly blobs = new Map<string, { bytes: Uint8Array; etag: string }>();
  readonly puts: { pathname: string; options: Record<string, unknown> }[] = [];
  beforeWrite: ((pathname: string) => void) | null = null;
  private written = 0;

  store(pathname: string, bytes: Uint8Array): void {
    this.written += 1;
    this.blobs.set(pathname, { bytes, etag: `"etag-${this.written}"` });
  }

  client() {
    const get = async (pathname: string) => {
      const stored = this.blobs.get(pathname);
      if (!stored) return null;
      return {
        statusCode: 200,
        stream: new Response(Buffer.from(stored.bytes)).body,
        headers: new Headers(),
        blob: { pathname, etag: stored.etag, contentType: "application/octet-stream", size: stored.bytes.byteLength },
      };
    };
    const put = async (
      pathname: string,
      body: string | Buffer,
      options: { allowOverwrite?: boolean; ifMatch?: string } & Record<string, unknown>,
    ) => {
      this.puts.push({ pathname, options });
      this.beforeWrite?.(pathname);
      const existing = this.blobs.get(pathname);
      if (existing && !options.allowOverwrite) throw new Error("The blob already exists.");
      if (options.ifMatch && existing?.etag !== options.ifMatch) throw new BlobPreconditionFailedError();
      this.store(pathname, typeof body === "string" ? new TextEncoder().encode(body) : new Uint8Array(body));
      return { pathname };
    };
    return { get, put } as never;
  }

  json(pathname: string): any {
    return JSON.parse(new TextDecoder().decode(this.blobs.get(pathname)!.bytes));
  }
}

function localFile(name: string, content: string | Uint8Array) {
  const file = join(temporaryDirectory(), "copy");
  writeFileSync(file, content);
  return { name, file, hfBlobId: sha256(typeof content === "string" ? content : Buffer.from(content)).slice(0, 40) };
}

function structuredRun(fingerprint = FINGERPRINT) {
  return {
    runId: STRUCTURED_RUN,
    folderFingerprint: fingerprint,
    indexRow: null,
    files: ["run_manifest.json", "progress.json", "events.ndjson"].map((name) =>
      localFile(name, readFileSync(join(FIXTURES, name)))
    ),
  };
}

function schema1Run(status: string, updatedAt: string, fingerprint = FINGERPRINT) {
  return {
    runId: SCHEMA_1_RUN,
    folderFingerprint: fingerprint,
    indexRow: null,
    files: [
      localFile("run_manifest.json", JSON.stringify({ schema_version: 1, run_id: SCHEMA_1_RUN })),
      localFile(
        "progress.json",
        JSON.stringify({
          schema_version: 1,
          run_id: SCHEMA_1_RUN,
          candidate_release_id: "candidate-1",
          status,
          stage: "calibrating",
          started_at: "2026-11-18T09:00:00Z",
          updated_at: updatedAt,
        }),
      ),
    ],
  };
}

function publicationRequest(
  country: string,
  runs: unknown[],
  treeChecks: unknown[] = [],
  removedRunIds: string[] = [],
) {
  return { country, hfRepo: HF_REPO, hfCommitSha: HF_COMMIT, runs, treeChecks, removedRunIds };
}

async function publish(
  store: InMemoryBlobStore,
  country: string,
  runs: unknown[],
  treeChecks: unknown[] = [],
  removedRunIds: string[] = [],
) {
  const { parseStagingRunFilesPublication, publishStagingRunFiles } = await command();
  return publishStagingRunFiles({
    request: parseStagingRunFilesPublication(publicationRequest(country, runs, treeChecks, removedRunIds)),
    token: TOKEN,
    client: store.client(),
    now: () => new Date(NOW),
  });
}

test("copies live apart from the tree store's folders, each file under its own digest", async () => {
  const { stagingRunFilePath, stagingRunFilesIndexPath } = await runFiles();
  const digest = "b".repeat(64);

  expect(stagingRunFilesIndexPath("uk")).toBe("staging-run-files/uk/index.json");
  expect(stagingRunFilePath("uk", STRUCTURED_RUN, digest)).toBe(
    `staging-run-files/uk/runs/${STRUCTURED_RUN}/${digest}.gz`,
  );
  expect(() => stagingRunFilePath("uk", "../manifest", digest)).toThrow();
  expect(() => stagingRunFilePath("uk", STRUCTURED_RUN, "not-a-digest")).toThrow();
});

test("a run's files are stored privately and gzipped, and the index records them with the run's summary", async () => {
  const { stagingRunFilesIndexPath, stagingRunFilePath } = await runFiles();
  const store = new InMemoryBlobStore();
  const run = structuredRun();

  const answer = await publish(store, "uk", [run]);

  expect(answer.runs[STRUCTURED_RUN]).toEqual({ uploaded: 3, kept: 0 });
  const index = store.json(stagingRunFilesIndexPath("uk"));
  const record = index.runs[STRUCTURED_RUN];
  expect(record.hfCommitSha).toBe(HF_COMMIT);
  expect(record.copiedAt).toBe(NOW);
  expect(record.folderFingerprint).toBe(FINGERPRINT);
  expect(record.problem).toBeNull();
  expect(record.summary.status).toBe("failed");
  expect(record.summary.schema_version).toBe(3);
  for (const file of run.files) {
    const original = readFileSync(file.file);
    const stored = record.files[file.name];
    expect(stored.sha256).toBe(sha256(original));
    expect(stored.rawBytes).toBe(original.byteLength);
    expect(stored.hfBlobId).toBe(file.hfBlobId);
    expect(stored.path).toBe(stagingRunFilePath("uk", STRUCTURED_RUN, stored.sha256));
    expect(Buffer.from(gunzipSync(store.blobs.get(stored.path)!.bytes)).equals(original)).toBe(true);
    const options = store.puts.find((put) => put.pathname === stored.path)!.options;
    expect(options.access).toBe("private");
    expect(options.token).toBe(TOKEN);
    expect(options.contentType).toBe("application/gzip");
  }
  expect(store.puts.at(-1)?.pathname).toBe(stagingRunFilesIndexPath("uk"));
});

test("only a changed file is stored again, and the earlier version stays", async () => {
  const { stagingRunFilesIndexPath } = await runFiles();
  const store = new InMemoryBlobStore();
  await publish(store, "us", [schema1Run("running", "2026-11-18T10:00:00Z")]);
  const before = store.json(stagingRunFilesIndexPath("us")).runs[SCHEMA_1_RUN];
  store.puts.length = 0;

  const answer = await publish(store, "us", [schema1Run("failed", "2026-11-18T11:00:00Z", OTHER_FINGERPRINT)]);

  const after = store.json(stagingRunFilesIndexPath("us")).runs[SCHEMA_1_RUN];
  expect(answer.runs[SCHEMA_1_RUN]).toEqual({ uploaded: 1, kept: 1 });
  expect(store.puts.map((put) => put.pathname)).toEqual([
    after.files["progress.json"].path,
    stagingRunFilesIndexPath("us"),
  ]);
  expect(after.files["run_manifest.json"]).toEqual(before.files["run_manifest.json"]);
  expect(after.files["progress.json"].path).not.toBe(before.files["progress.json"].path);
  expect(store.blobs.has(before.files["progress.json"].path)).toBe(true);
  expect(after.folderFingerprint).toBe(OTHER_FINGERPRINT);
  expect(after.summary.status).toBe("failed");
});

test("a run's tree check is kept when its files are copied again, and set on request", async () => {
  const { stagingRunFilesIndexPath } = await runFiles();
  const store = new InMemoryBlobStore();
  const check = { runId: SCHEMA_1_RUN, folderFingerprint: FINGERPRINT, buildArtifactId: BUILD_ID };
  await publish(store, "us", [schema1Run("running", "2026-11-18T10:00:00Z")], [check]);
  expect(store.json(stagingRunFilesIndexPath("us")).runs[SCHEMA_1_RUN].treeCheck).toEqual({
    folderFingerprint: FINGERPRINT,
    buildArtifactId: BUILD_ID,
  });

  await publish(store, "us", [schema1Run("running", "2026-11-18T11:00:00Z", OTHER_FINGERPRINT)]);

  expect(store.json(stagingRunFilesIndexPath("us")).runs[SCHEMA_1_RUN].treeCheck.buildArtifactId).toBe(BUILD_ID);
});

test("a tree check can record why the run has no new tree", async () => {
  const { stagingRunFilesIndexPath } = await runFiles();
  const store = new InMemoryBlobStore();
  const check = {
    runId: SCHEMA_1_RUN,
    folderFingerprint: FINGERPRINT,
    buildArtifactId: null,
    outcome: "refused",
    reason: "The publication refused the tree.",
    sourceFileConflicts: ["reformValidation"],
  };

  await publish(store, "us", [schema1Run("failed", "2026-11-18T10:00:00Z")], [check]);

  expect(store.json(stagingRunFilesIndexPath("us")).runs[SCHEMA_1_RUN].treeCheck).toEqual({
    folderFingerprint: FINGERPRINT,
    buildArtifactId: null,
    outcome: "refused",
    reason: "The publication refused the tree.",
    sourceFileConflicts: ["reformValidation"],
  });
});

test("a run removed from Hugging Face leaves the readers and returns with its folder", async () => {
  const { loadCopiedStagingRun, loadCopiedStagingRuns, stagingRunFilesIndexPath } = await runFiles();
  const store = new InMemoryBlobStore();
  const request = { country: "us" as const, token: TOKEN, client: store.client() };
  await publish(store, "us", [schema1Run("failed", "2026-11-18T10:00:00Z")]);

  await publish(store, "us", [], [], [SCHEMA_1_RUN]);

  expect(store.json(stagingRunFilesIndexPath("us")).runs[SCHEMA_1_RUN].removedAt).toBe(NOW);
  expect((await loadCopiedStagingRuns(request)).runs).toEqual([]);
  expect(await loadCopiedStagingRun({ ...request, runId: SCHEMA_1_RUN })).toBeNull();

  await publish(store, "us", [schema1Run("failed", "2026-11-18T10:00:00Z")]);

  expect(store.json(stagingRunFilesIndexPath("us")).runs[SCHEMA_1_RUN].removedAt).toBeUndefined();
  expect((await loadCopiedStagingRuns(request)).runs.map((run: { run_id: string }) => run.run_id)).toEqual([
    SCHEMA_1_RUN,
  ]);
});

test("a file too large to copy is named, so a reader can tell it from an absent one", async () => {
  const { loadCopiedStagingRun, stagingRunFilesIndexPath } = await runFiles();
  const store = new InMemoryBlobStore();
  await publish(store, "uk", [{ ...structuredRun(), omittedFiles: ["calibration_progress.json"] }]);

  expect(store.json(stagingRunFilesIndexPath("uk")).runs[STRUCTURED_RUN].omittedFiles).toEqual([
    "calibration_progress.json",
  ]);
  const copied = await loadCopiedStagingRun({
    country: "uk",
    runId: STRUCTURED_RUN,
    token: TOKEN,
    client: store.client(),
  });
  expect(copied?.omitted_files).toEqual(["calibration_progress.json"]);
});

test("malformed optional files read as absent, as the Hugging Face reader takes them", async () => {
  const { loadCopiedStagingRun } = await runFiles();
  const store = new InMemoryBlobStore();
  const run = structuredRun();
  run.files.push(localFile("calibration_progress.json", "not json"), localFile("reform_validation.json", "[]"));
  await publish(store, "uk", [run]);

  const copied = await loadCopiedStagingRun({
    country: "uk",
    runId: STRUCTURED_RUN,
    token: TOKEN,
    client: store.client(),
  });

  expect(copied?.calibration_progress).toBeNull();
  expect(copied?.reform_validation).toBeNull();
});

test("a list entry reads progress only where the Hugging Face reader does", async () => {
  const { stagingRunFilesIndexPath } = await runFiles();
  const store = new InMemoryBlobStore();
  const structured = structuredRun();
  structured.files[1] = localFile("progress.json", "not json");
  const schema1 = schema1Run("running", "2026-11-18T10:00:00Z");
  schema1.files[1] = localFile("progress.json", "not json");

  await publish(store, "us", [schema1]);
  await publish(store, "uk", [structured]);

  const usRecord = store.json(stagingRunFilesIndexPath("us")).runs[SCHEMA_1_RUN];
  expect(usRecord.problem).toBeNull();
  expect(usRecord.summary.status).toBeNull();
  const ukRecord = store.json(stagingRunFilesIndexPath("uk")).runs[STRUCTURED_RUN];
  expect(ukRecord.problem).toBeNull();
  expect(ukRecord.summary.status).toBe("failed");
});

test("a schema-1 run's runs.json row comes before its progress, as in the Hugging Face reader", async () => {
  const { stagingRunFilesIndexPath } = await runFiles();
  const store = new InMemoryBlobStore();
  const run = schema1Run("running", "2026-11-18T10:00:00Z");
  run.files[1] = localFile("progress.json", JSON.stringify({ schema_version: 1 }));

  await publish(store, "us", [
    { ...run, indexRow: { run_id: SCHEMA_1_RUN, candidate_release_id: "candidate-3", status: "passed" } },
  ]);

  const { summary, problem } = store.json(stagingRunFilesIndexPath("us")).runs[SCHEMA_1_RUN];
  expect(problem).toBeNull();
  expect(summary.status).toBe("passed");
  expect(summary.candidate_release_id).toBe("candidate-3");
});

test("a tree check for a run without a copy changes nothing", async () => {
  const { stagingRunFilesIndexPath } = await runFiles();
  const store = new InMemoryBlobStore();

  await publish(store, "us", [], [{ runId: SCHEMA_1_RUN, folderFingerprint: FINGERPRINT, buildArtifactId: null }]);

  expect(store.json(stagingRunFilesIndexPath("us")).runs).toEqual({});
});

test("a run whose documents break the contract is still copied, with the problem recorded", async () => {
  const { loadCopiedStagingRuns, stagingRunFilesIndexPath } = await runFiles();
  const store = new InMemoryBlobStore();
  const broken = {
    runId: SCHEMA_1_RUN,
    folderFingerprint: FINGERPRINT,
    indexRow: null,
    files: [localFile("run_manifest.json", "not json")],
  };

  await publish(store, "us", [broken]);

  const record = store.json(stagingRunFilesIndexPath("us")).runs[SCHEMA_1_RUN];
  expect(record.files["run_manifest.json"]).toBeDefined();
  expect(record.problem).toContain("run_manifest.json");
  const listed = await loadCopiedStagingRuns({ country: "us", token: TOKEN, client: store.client() });
  expect(listed.runs).toEqual([]);
  expect(listed.incompatible_runs.map((run: { run_id: string }) => run.run_id)).toEqual([SCHEMA_1_RUN]);
});

test("a schema-1 run with only a runs.json row takes its summary from that row", async () => {
  const { stagingRunFilesIndexPath } = await runFiles();
  const store = new InMemoryBlobStore();
  const rowOnly = {
    runId: SCHEMA_1_RUN,
    folderFingerprint: FINGERPRINT,
    indexRow: { run_id: SCHEMA_1_RUN, candidate_release_id: "candidate-2", status: "passed" },
    files: [localFile("events.ndjson", "")],
  };

  await publish(store, "us", [rowOnly]);

  const { summary, problem } = store.json(stagingRunFilesIndexPath("us")).runs[SCHEMA_1_RUN];
  expect(problem).toBeNull();
  expect(summary.status).toBe("passed");
  expect(summary.candidate_release_id).toBe("candidate-2");
});

test("an index another writer changed is not overwritten", async () => {
  const { stagingRunFilesIndexPath } = await runFiles();
  const store = new InMemoryBlobStore();
  await publish(store, "us", [schema1Run("running", "2026-11-18T10:00:00Z")]);
  const indexPath = stagingRunFilesIndexPath("us");
  store.beforeWrite = (pathname) => {
    if (pathname === indexPath) store.store(indexPath, new TextEncoder().encode('{"changed":true}'));
    store.beforeWrite = pathname === indexPath ? null : store.beforeWrite;
  };

  await expect(publish(store, "us", [schema1Run("failed", "2026-11-18T11:00:00Z")])).rejects.toThrow();

  expect(store.json(indexPath)).toEqual({ changed: true });
});

test("a publication request is refused for an unknown file, an unsafe run or a bad fingerprint", async () => {
  const { parseStagingRunFilesPublication } = await command();
  const file = { name: "progress.json", file: "/tmp/copy", hfBlobId: "c".repeat(40) };
  const run = { runId: SCHEMA_1_RUN, folderFingerprint: FINGERPRINT, indexRow: null, files: [file] };

  expect(() => parseStagingRunFilesPublication(publicationRequest("us", [run]))).not.toThrow();
  for (const bad of [
    { ...run, runId: "../manifest" },
    { ...run, folderFingerprint: "short" },
    { ...run, files: [{ ...file, name: "calibration_diagnostics.json" }] },
    { ...run, files: [file, file] },
  ]) {
    expect(() => parseStagingRunFilesPublication(publicationRequest("us", [bad]))).toThrow();
  }
  expect(() => parseStagingRunFilesPublication(publicationRequest("xx", [run]))).toThrow();
});

test("the run list is read from the index, newest first, without reading any run's files", async () => {
  const { loadCopiedStagingRuns, stagingRunFilesIndexPath } = await runFiles();
  const store = new InMemoryBlobStore();
  await publish(store, "uk", [structuredRun(), schema1Run("running", "2030-01-01T00:00:00Z")]);
  const reads: string[] = [];
  const client = store.client() as { get: (pathname: string) => Promise<unknown> };
  const recording = {
    ...client,
    get: (pathname: string) => {
      reads.push(pathname);
      return client.get(pathname);
    },
  };

  const listed = await loadCopiedStagingRuns({ country: "uk", token: TOKEN, client: recording as never });

  expect(listed.available).toBe(true);
  expect(listed.source_repo).toBe(HF_REPO);
  expect(listed.runs.map((run: { run_id: string }) => run.run_id)).toEqual([SCHEMA_1_RUN, STRUCTURED_RUN]);
  expect(reads).toEqual([stagingRunFilesIndexPath("uk")]);
});

test("a country with no copies has an empty run list", async () => {
  const { loadCopiedStagingRuns } = await runFiles();

  const listed = await loadCopiedStagingRuns({ country: "uk", token: TOKEN, client: new InMemoryBlobStore().client() });

  expect(listed.runs).toEqual([]);
  expect(listed.incompatible_runs).toEqual([]);
});

test("a copied run gives back its documents as the Hugging Face reader parses them", async () => {
  const { loadCopiedStagingRun } = await runFiles();
  const store = new InMemoryBlobStore();
  await publish(store, "uk", [structuredRun()]);

  const copied = await loadCopiedStagingRun({
    country: "uk",
    runId: STRUCTURED_RUN,
    token: TOKEN,
    client: store.client(),
  });

  expect(copied?.run_id).toBe(STRUCTURED_RUN);
  expect(copied?.hf_commit_sha).toBe(HF_COMMIT);
  expect(copied?.copied_at).toBe(NOW);
  expect(copied?.progress?.status).toBe("failed");
  expect(copied?.run_manifest?.run_id).toBe(STRUCTURED_RUN);
  expect(copied?.calibration_progress).toBeNull();
  expect(copied?.events.length).toBeGreaterThan(0);
  expect(copied?.reform_validation).toBeNull();
  expect(copied?.staged_dataset).toBeNull();
});

test("a run with no copy is null, and a stored file that differs from its digest is refused", async () => {
  const { loadCopiedStagingRun, stagingRunFilesIndexPath } = await runFiles();
  const store = new InMemoryBlobStore();
  await publish(store, "uk", [structuredRun()]);
  const request = { country: "uk" as const, token: TOKEN, client: store.client() };

  expect(await loadCopiedStagingRun({ ...request, runId: "another-run" })).toBeNull();

  const { path } = store.json(stagingRunFilesIndexPath("uk")).runs[STRUCTURED_RUN].files["progress.json"];
  store.store(path, gzipSync("{}"));
  await expect(loadCopiedStagingRun({ ...request, runId: STRUCTURED_RUN })).rejects.toThrow("digest");
});

test("the command prints the country's index, an empty one while there is none", async () => {
  const { stagingRunFilesCommandMain } = await command();
  const store = new InMemoryBlobStore();
  const printed: string[] = [];
  const log = console.log;
  console.log = (text: string) => printed.push(text);
  try {
    const exitCode = await stagingRunFilesCommandMain(["read", "uk"], { [TOKEN_VARIABLE]: TOKEN }, store.client());
    expect(exitCode).toBe(0);
  } finally {
    console.log = log;
  }

  expect(JSON.parse(printed[0]).runs).toEqual({});
  expect(store.puts).toEqual([]);
});

test("the command prints the source files a stored tree was built from", async () => {
  const { stagingRunFilesCommandMain } = await command();
  const store = new InMemoryBlobStore();
  const sources = {
    calibrationDiagnostics: { path: "runs/run/calibration_diagnostics.json", sha256: "d".repeat(64) },
    buildManifest: null,
  };
  store.store(
    `calibration-trees/uk/${BUILD_ID}/index.json.gz`,
    gzipSync(JSON.stringify({ build: { sourceArtifacts: sources } })),
  );
  const printed: string[] = [];
  const log = console.log;
  console.log = (text: string) => printed.push(text);
  try {
    const exitCode = await stagingRunFilesCommandMain(
      ["tree-sources", "uk", BUILD_ID],
      { [TOKEN_VARIABLE]: TOKEN },
      store.client(),
    );
    expect(exitCode).toBe(0);
  } finally {
    console.log = log;
  }

  expect(JSON.parse(printed[0])).toEqual(sources);
});

test("the command exits 2 for the sources of a tree the store holds no index for", async () => {
  const { stagingRunFilesCommandMain } = await command();
  const store = new InMemoryBlobStore();
  const error = console.error;
  console.error = () => undefined;
  try {
    const exitCode = await stagingRunFilesCommandMain(
      ["tree-sources", "uk", BUILD_ID],
      { [TOKEN_VARIABLE]: TOKEN },
      store.client(),
    );
    expect(exitCode).toBe(2);
  } finally {
    console.error = error;
  }
});

test("the command exits 1 for a request it cannot follow and 3 when the store fails", async () => {
  const { stagingRunFilesCommandMain } = await command();
  const failing = { get: async () => { throw new Error("The store is unavailable."); } } as never;
  const error = console.error;
  console.error = () => undefined;
  try {
    expect(await stagingRunFilesCommandMain(["read", "uk"], {}, failing)).toBe(1);
    expect(await stagingRunFilesCommandMain(["unknown"], { [TOKEN_VARIABLE]: TOKEN }, failing)).toBe(1);
    expect(await stagingRunFilesCommandMain(["read", "uk"], { [TOKEN_VARIABLE]: TOKEN }, failing)).toBe(3);
  } finally {
    console.error = error;
  }
});

// US builds move to Microcosm's version 2 writer (microcosm#1135), which puts
// the reform validation at artifacts/reform_validation.json; version 1 runs
// put it at the run folder's top level.
test("a US version 2 run's reform validation is copied under its artifacts path and read back", async () => {
  const { isStagingRunFileName, loadCopiedStagingRun, stagingRunFilesIndexPath } = await runFiles();
  expect(isStagingRunFileName("artifacts/reform_validation.json")).toBe(true);
  const folder = usVersion2RunFolder();
  const names = ["run_manifest.json", "progress.json", "events.ndjson", "artifacts/reform_validation.json"];
  const store = new InMemoryBlobStore();

  await publish(store, "us", [{
    runId: US_VERSION_2_RUN_ID,
    folderFingerprint: FINGERPRINT,
    indexRow: null,
    files: names.map((name) => localFile(name, folder[name]!)),
  }]);

  const record = store.json(stagingRunFilesIndexPath("us")).runs[US_VERSION_2_RUN_ID];
  expect(record.problem).toBeNull();
  expect(record.summary).toMatchObject({
    run_id: US_VERSION_2_RUN_ID,
    country_code: "US",
    schema_version: 2,
    status: "completed",
  });
  expect(Object.keys(record.files).sort()).toEqual([...names].sort());
  const copied = await loadCopiedStagingRun({
    country: "us",
    runId: US_VERSION_2_RUN_ID,
    token: TOKEN,
    client: store.client(),
  });
  expect(copied?.reform_validation).toEqual(JSON.parse(folder["artifacts/reform_validation.json"]!));
});

test("a declared reform validation copy that differs from its declared digest reads as absent", async () => {
  const { isStagingRunFileName, loadCopiedStagingRun } = await runFiles();
  expect(isStagingRunFileName("artifacts/reform_validation.json")).toBe(true);
  const folder = usVersion2RunFolder();
  const store = new InMemoryBlobStore();

  await publish(store, "us", [{
    runId: US_VERSION_2_RUN_ID,
    folderFingerprint: FINGERPRINT,
    indexRow: null,
    files: [
      ...["run_manifest.json", "progress.json"].map((name) => localFile(name, folder[name]!)),
      localFile("artifacts/reform_validation.json", JSON.stringify({ reforms: ["another"] })),
      // A top-level copy beside it is not what the manifest declares.
      localFile("reform_validation.json", folder["artifacts/reform_validation.json"]!),
    ],
  }]);

  const copied = await loadCopiedStagingRun({
    country: "us",
    runId: US_VERSION_2_RUN_ID,
    token: TOKEN,
    client: store.client(),
  });
  expect(copied?.progress).not.toBeNull();
  expect(copied?.reform_validation).toBeNull();
});

// A collector deployed before a dashboard's package update may record files
// the dashboard's version does not know; its readers leave them out.
test("an index naming files this version does not copy is still read, without them", async () => {
  const { loadCopiedStagingRun, loadCopiedStagingRuns, stagingRunFilesIndexPath } = await runFiles();
  const store = new InMemoryBlobStore();
  await publish(store, "uk", [structuredRun()]);
  const index = store.json(stagingRunFilesIndexPath("uk"));
  const record = index.runs[STRUCTURED_RUN];
  record.files["artifacts/a_later_result.json"] = { ...record.files["progress.json"] };
  record.omittedFiles = ["artifacts/another_later_result.json"];
  store.store(stagingRunFilesIndexPath("uk"), new TextEncoder().encode(JSON.stringify(index)));
  const request = { country: "uk" as const, token: TOKEN, client: store.client() };

  expect((await loadCopiedStagingRuns(request)).runs.map((run: { run_id: string }) => run.run_id)).toEqual([
    STRUCTURED_RUN,
  ]);
  const copied = await loadCopiedStagingRun({ ...request, runId: STRUCTURED_RUN });
  expect(copied?.progress).not.toBeNull();
  expect(copied?.reform_validation).toBeNull();
});

test("a run's reform validation copy is the one its structured manifest declares, else the top-level one", async () => {
  const { stagingReformValidationFileName } = await runFiles();
  const { reform_validation: _reformValidation, ...undeclared } = usVersion2ResultFiles();
  const manifest = (folder: Record<string, string>) => parseStagingManifest(JSON.parse(folder["run_manifest.json"]!));

  expect(stagingReformValidationFileName(manifest(usVersion2RunFolder()))).toBe("artifacts/reform_validation.json");
  expect(stagingReformValidationFileName(manifest(usVersion2RunFolder({ resultFiles: undeclared })))).toBe(
    "reform_validation.json",
  );
  expect(stagingReformValidationFileName({ schema_version: 1, run_id: SCHEMA_1_RUN })).toBe("reform_validation.json");
  expect(stagingReformValidationFileName(null)).toBe("reform_validation.json");
});
