// Reading the collector's calibration tree manifest for the backfill, which
// compares it with production's: the read command prints the stored manifest
// as the manifest's own parser reads it, read without the cache, and never
// writes to the store. The store here is an in-memory stand-in.
import { afterEach, expect, spyOn, test } from "bun:test";
import { existsSync } from "node:fs";
import { join } from "node:path";

import { CALIBRATION_TREE_SCHEMA_VERSION } from "./calibration-tree-artifact";
import type { CalibrationTreeBlobClient } from "./calibration-tree-blob";
import {
  CALIBRATION_TREE_MANIFEST_PATH,
  CALIBRATION_TREE_MANIFEST_SCHEMA_VERSION,
  type CalibrationTreeManifest,
  emptyCalibrationTreeManifest,
} from "./calibration-tree-manifest";
import * as manifestReadCommand from "./calibration-tree-manifest-read";

const READ_COMMAND = join(import.meta.dir, "calibration-tree-manifest-read.ts");
const TOKEN = "vercel_blob_rw_test_token";
const TOKEN_VARIABLE = "CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN";
const BUILD_ARTIFACT_ID = "a".repeat(64);

const STORED_MANIFEST: CalibrationTreeManifest = {
  schemaVersion: CALIBRATION_TREE_MANIFEST_SCHEMA_VERSION,
  countries: {
    us: {
      latestReleaseBuildArtifactId: BUILD_ARTIFACT_ID,
      builds: [
        {
          buildArtifactId: BUILD_ARTIFACT_ID,
          kind: "release",
          sourceId: "microcosm-us-2025",
          label: "microcosm-us-2025",
          releaseId: "microcosm-us-2025",
          stagingRunId: null,
          hfRepo: "policyengine/populace-us",
          hfCommitSha: "1".repeat(40),
          treeSchemaVersion: CALIBRATION_TREE_SCHEMA_VERSION,
          indexSha256: "e".repeat(64),
          indexBytes: 1000,
          createdAt: "2026-10-20T09:00:00.000Z",
          updatedAt: "2026-10-20T09:00:00.000Z",
        },
      ],
    },
  },
};

interface RecordedGet {
  pathname: string;
  useCache: boolean | undefined;
  token: string | undefined;
}

// Answers reads of the manifest from memory and records every write.
class InMemoryManifestStore {
  manifestText: string | null = null;
  failure: Error | null = null;
  readonly gets: RecordedGet[] = [];
  readonly puts: string[] = [];

  client(): CalibrationTreeBlobClient {
    const get = async (pathname: string, options: { useCache?: boolean; token?: string }) => {
      this.gets.push({ pathname, useCache: options.useCache, token: options.token });
      if (this.failure) throw this.failure;
      if (this.manifestText === null) return null;
      const bytes = new TextEncoder().encode(this.manifestText);
      return {
        statusCode: 200,
        stream: new Response(bytes).body,
        headers: new Headers(),
        blob: { pathname, etag: '"etag-1"', contentType: "application/json", size: bytes.byteLength },
      };
    };
    const put = async (pathname: string) => {
      this.puts.push(pathname);
      return { pathname };
    };
    return { get, put } as unknown as CalibrationTreeBlobClient;
  }
}

const consoleSpies: Array<{ mockRestore(): void }> = [];

afterEach(() => {
  for (const spy of consoleSpies.splice(0)) spy.mockRestore();
});

function capturedConsole() {
  const printed: string[] = [];
  const errors: string[] = [];
  const logSpy = spyOn(console, "log").mockImplementation((...values: unknown[]) => {
    printed.push(values.map(String).join(" "));
  });
  const errorSpy = spyOn(console, "error").mockImplementation((...values: unknown[]) => {
    errors.push(values.map(String).join(" "));
  });
  consoleSpies.push(logSpy, errorSpy);
  return { printed, errors };
}

test("the stored manifest is read without the cache, as its parser reads it", async () => {
  const { readStoredCalibrationTreeManifest } = manifestReadCommand;
  const store = new InMemoryManifestStore();
  store.manifestText = JSON.stringify(STORED_MANIFEST);

  const manifest = await readStoredCalibrationTreeManifest({ token: TOKEN, client: store.client() });

  expect(manifest).toEqual(STORED_MANIFEST);
  expect(store.gets).toEqual([{ pathname: CALIBRATION_TREE_MANIFEST_PATH, useCache: false, token: TOKEN }]);
  expect(store.puts).toEqual([]);
});

test("a store without a manifest yet reads as the empty manifest", async () => {
  const { readStoredCalibrationTreeManifest } = manifestReadCommand;
  const store = new InMemoryManifestStore();

  const manifest = await readStoredCalibrationTreeManifest({ token: TOKEN, client: store.client() });

  expect(manifest).toEqual(emptyCalibrationTreeManifest());
  expect(store.puts).toEqual([]);
});

test("the command prints the manifest as one JSON line and exits 0", async () => {
  const { calibrationTreeManifestReadMain } = manifestReadCommand;
  const store = new InMemoryManifestStore();
  store.manifestText = JSON.stringify(STORED_MANIFEST);
  const output = capturedConsole();

  const exitCode = await calibrationTreeManifestReadMain([], { [TOKEN_VARIABLE]: TOKEN }, store.client());

  expect(exitCode).toBe(0);
  expect(output.printed).toHaveLength(1);
  expect(output.printed[0]).not.toContain("\n");
  expect(JSON.parse(output.printed[0])).toEqual(STORED_MANIFEST);
  expect(store.puts).toEqual([]);
});

test("a manifest that breaks the manifest's rules, or a store that fails, exits 3", async () => {
  const malformed = new InMemoryManifestStore();
  malformed.manifestText = JSON.stringify({ ...STORED_MANIFEST, schemaVersion: 5 });
  const failing = new InMemoryManifestStore();
  failing.failure = new Error("The store is unavailable.");
  const output = capturedConsole();

  const malformedExit = await manifestReadCommand.calibrationTreeManifestReadMain([], { [TOKEN_VARIABLE]: TOKEN }, malformed.client());
  const failingExit = await manifestReadCommand.calibrationTreeManifestReadMain([], { [TOKEN_VARIABLE]: TOKEN }, failing.client());

  expect(malformedExit).toBe(manifestReadCommand.CALIBRATION_TREE_MANIFEST_READ_EXIT_STORE_ERROR);
  expect(failingExit).toBe(manifestReadCommand.CALIBRATION_TREE_MANIFEST_READ_EXIT_STORE_ERROR);
  expect(manifestReadCommand.CALIBRATION_TREE_MANIFEST_READ_EXIT_STORE_ERROR).toBe(3);
  expect(output.printed).toEqual([]);
  expect(output.errors.join("\n")).not.toContain(TOKEN);
});

test("the command refuses to run without its token or with arguments", async () => {
  const store = new InMemoryManifestStore();
  const output = capturedConsole();

  const withoutToken = await manifestReadCommand.calibrationTreeManifestReadMain([], {}, store.client());
  const withArguments = await manifestReadCommand.calibrationTreeManifestReadMain(
    ["extra.json"],
    { [TOKEN_VARIABLE]: TOKEN },
    store.client(),
  );

  expect(withoutToken).toBe(manifestReadCommand.CALIBRATION_TREE_MANIFEST_READ_EXIT_REQUEST_ERROR);
  expect(withArguments).toBe(manifestReadCommand.CALIBRATION_TREE_MANIFEST_READ_EXIT_REQUEST_ERROR);
  expect(manifestReadCommand.CALIBRATION_TREE_MANIFEST_READ_EXIT_REQUEST_ERROR).toBe(1);
  expect(output.errors.join("\n")).toContain(TOKEN_VARIABLE);
  expect(store.gets).toEqual([]);
});

test("run as its own process without a token, the command exits 1 and reads nothing", () => {
  expect(existsSync(READ_COMMAND)).toBe(true);

  const run = Bun.spawnSync({
    cmd: [process.execPath, "run", READ_COMMAND],
    env: { PATH: process.env.PATH ?? "" },
    stdout: "pipe",
    stderr: "pipe",
  });

  expect(run.exitCode).toBe(1);
  expect(run.stdout.toString()).toBe("");
  expect(run.stderr.toString()).toContain(TOKEN_VARIABLE);
});
