// The calibration tree worker, run the way the collector runs it: as its own
// Bun process, reading a request file and the build's files from disk and
// writing the tree's gzip parts to a directory. For every recorded release it
// must write exactly the bytes the dashboard's builder recorded.
import { afterEach, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { gunzipSync } from "node:zlib";

import recordedGoldens from "./fixtures/calibration-tree-golden.json";
import { microcosmRepo } from "@policyengine/calibration-tree/latest-artifact";
import {
  GZIP_HEADER_WITHOUT_OS,
  goldenReleaseInputs,
  type CalibrationTreeGoldenRecord,
  type GoldenFileRecord,
  type GoldenReleaseInput,
} from "./test-support/calibration-tree-golden-cases";

const recorded = recordedGoldens as CalibrationTreeGoldenRecord;
const WORKER = join(import.meta.dir, "calibration-tree-worker.ts");
// The gzip header is ten bytes; the last one names the operating system.
const GZIP_HEADER_BYTES = 10;
// The US release has 5,659 targets; generous for a heavily loaded machine.
const BUILD_TIMEOUT_MS = 300_000;
const EXIT_REQUEST_ERROR = 1;
const EXIT_UNBUILDABLE_INPUT = 2;

type ReleaseCaseName = keyof ReturnType<typeof goldenReleaseInputs>;
const RELEASE_CASES = Object.keys(goldenReleaseInputs()) as ReleaseCaseName[];

const temporaryDirectories: string[] = [];

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function temporaryDirectory(): string {
  const directory = mkdtempSync(join(tmpdir(), "calibration-tree-worker-"));
  temporaryDirectories.push(directory);
  return directory;
}

function sha256(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

interface WorkerRun {
  exitCode: number;
  stdout: string;
  stderr: string;
}

// Runs the worker with only PATH and HOME, so no repository override, size
// limit or token from the test's own environment reaches it.
function runWorker(requestPath: string, outputDirectory: string, environment: Record<string, string> = {}): WorkerRun {
  const result = Bun.spawnSync({
    cmd: [process.execPath, "run", WORKER, requestPath, outputDirectory],
    env: { PATH: process.env.PATH ?? "", HOME: process.env.HOME ?? "", ...environment },
    stdout: "pipe",
    stderr: "pipe",
  });
  return {
    exitCode: result.exitCode ?? -1,
    stdout: result.stdout.toString(),
    stderr: result.stderr.toString(),
  };
}

// Writes a recorded release's files where a submission's files would be
// downloaded, and a request naming them, as the publisher would read them
// from the release folder.
function releaseRequest(
  release: GoldenReleaseInput,
  overrides: Record<string, unknown> = {},
): { requestPath: string; inputDirectory: string } {
  const inputDirectory = temporaryDirectory();
  const prefix = `releases/${release.releaseId}`;
  writeFileSync(join(inputDirectory, "calibration_diagnostics.json"), release.diagnosticsText);
  if (release.releaseManifestText != null) {
    writeFileSync(join(inputDirectory, "release_manifest.json"), release.releaseManifestText);
  }
  const request = {
    kind: "release",
    country: release.country,
    releaseId: release.releaseId,
    releaseDirectory: release.releaseId,
    hfRepo: microcosmRepo(release.country),
    hfCommitSha: release.hfCommitSha,
    createdAt: release.publishedAt,
    artifacts: {
      calibrationDiagnostics: {
        path: `${prefix}/calibration_diagnostics.json`,
        file: join(inputDirectory, "calibration_diagnostics.json"),
      },
      buildManifest: null,
      releaseManifest: release.releaseManifestText == null
        ? null
        : {
            path: `${prefix}/release_manifest.json`,
            file: join(inputDirectory, "release_manifest.json"),
          },
      demographics: null,
    },
    ...overrides,
  };
  const requestPath = join(inputDirectory, "request.json");
  writeFileSync(requestPath, JSON.stringify(request));
  return { requestPath, inputDirectory };
}

function writtenFiles(outputDirectory: string): string[] {
  if (!existsSync(outputDirectory)) return [];
  return readdirSync(outputDirectory, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile())
    .map((entry) => relative(outputDirectory, join(entry.parentPath, entry.name)))
    .sort();
}

// What the golden record keeps of a part, computed from the bytes on disk.
function writtenFileRecord(outputDirectory: string, part: string, path: string): GoldenFileRecord {
  const compressed = new Uint8Array(readFileSync(join(outputDirectory, path)));
  const raw = gunzipSync(compressed);
  return {
    part,
    path,
    sha256: sha256(raw),
    rawBytes: raw.byteLength,
    gzipBytes: compressed.byteLength,
    deflateSha256: sha256(compressed.subarray(GZIP_HEADER_BYTES)),
  };
}

interface WorkerSummary {
  buildArtifactId: string;
  index: { path: string; sha256: string; rawBytes: number; gzipBytes: number };
  files: Array<{ part: string; path: string; sha256: string; rawBytes: number; gzipBytes: number }>;
}

for (const name of RELEASE_CASES) {
  test(
    `the worker writes the recorded bytes of the ${name} tree`,
    () => {
      const { requestPath } = releaseRequest(goldenReleaseInputs()[name]);
      const outputDirectory = join(temporaryDirectory(), "trees");

      const run = runWorker(requestPath, outputDirectory);

      expect({ exitCode: run.exitCode, stderr: run.stderr }).toEqual({ exitCode: 0, stderr: "" });
      const expected = recorded.sources[name];
      const summary = JSON.parse(run.stdout) as WorkerSummary;
      expect(summary.buildArtifactId).toBe(expected.buildArtifactId);
      expect(summary.files.map((file) => writtenFileRecord(outputDirectory, file.part, file.path)))
        .toEqual(expected.files);
      expect(summary.files).toEqual(expected.files.map(({ deflateSha256: _, ...file }) => file));
      const index = expected.files.find((file) => file.part === "index")!;
      expect(summary.index).toEqual({
        path: index.path,
        sha256: index.sha256,
        rawBytes: index.rawBytes,
        gzipBytes: index.gzipBytes,
      });
      expect(writtenFiles(outputDirectory)).toEqual(expected.files.map((file) => file.path).sort());
      for (const file of expected.files) {
        const header = readFileSync(join(outputDirectory, file.path)).subarray(0, GZIP_HEADER_WITHOUT_OS.length);
        expect([...header]).toEqual(GZIP_HEADER_WITHOUT_OS);
      }
    },
    BUILD_TIMEOUT_MS,
  );
}

function writtenIndex(outputDirectory: string, summary: WorkerSummary): {
  build: { releaseId: string; comparison: { releaseId: string } | null };
} {
  return JSON.parse(gunzipSync(readFileSync(join(outputDirectory, summary.index.path))).toString("utf8"));
}

test("a build folder's tree compares under the build folder, as the publisher records it", () => {
  const release = goldenReleaseInputs()["uk-release"];
  const buildFolder = `${release.releaseId}/uk-frs-calibration-attempt-20261019T083323Z-6cba5d71`;
  const { requestPath } = releaseRequest(release, { releaseId: buildFolder, releaseDirectory: buildFolder });
  const outputDirectory = join(temporaryDirectory(), "trees");

  const run = runWorker(requestPath, outputDirectory);

  expect({ exitCode: run.exitCode, stderr: run.stderr }).toEqual({ exitCode: 0, stderr: "" });
  const index = writtenIndex(outputDirectory, JSON.parse(run.stdout) as WorkerSummary);
  expect(index.build.releaseId).toBe(buildFolder);
  expect(index.build.comparison?.releaseId).toBe(buildFolder);
});

test("a tag read from its release's folder compares under the tag", () => {
  const release = goldenReleaseInputs()["uk-release"];
  const tag = `${release.releaseId}-20261004`;
  const { requestPath } = releaseRequest(release, { releaseId: tag, releaseDirectory: release.releaseId });
  const outputDirectory = join(temporaryDirectory(), "trees");

  const run = runWorker(requestPath, outputDirectory);

  expect({ exitCode: run.exitCode, stderr: run.stderr }).toEqual({ exitCode: 0, stderr: "" });
  const index = writtenIndex(outputDirectory, JSON.parse(run.stdout) as WorkerSummary);
  expect(index.build.releaseId).toBe(tag);
  expect(index.build.comparison?.releaseId).toBe(tag);
});

test("a release folder compares under the release its diagnostics name", () => {
  const release = goldenReleaseInputs()["uk-release"];
  const folder = `${release.releaseId}-folder`;
  const { requestPath } = releaseRequest(release, { releaseId: folder, releaseDirectory: folder });
  const outputDirectory = join(temporaryDirectory(), "trees");

  const run = runWorker(requestPath, outputDirectory);

  expect({ exitCode: run.exitCode, stderr: run.stderr }).toEqual({ exitCode: 0, stderr: "" });
  const index = writtenIndex(outputDirectory, JSON.parse(run.stdout) as WorkerSummary);
  expect(index.build.releaseId).toBe(folder);
  expect(index.build.comparison?.releaseId).toBe(release.releaseId);
});

test("diagnostics that are not a JSON object are refused as unbuildable and nothing is written", () => {
  const release = { ...goldenReleaseInputs()["be-release"], diagnosticsText: "[1, 2, 3]" };
  const { requestPath } = releaseRequest(release);
  const outputDirectory = join(temporaryDirectory(), "trees");

  const run = runWorker(requestPath, outputDirectory);

  expect(run.exitCode).toBe(EXIT_UNBUILDABLE_INPUT);
  expect(run.stderr).toContain("calibration_diagnostics.json must be a JSON object.");
  expect(run.stdout).toBe("");
  expect(writtenFiles(outputDirectory)).toEqual([]);
});

test("a part over the publisher's configured size limit is refused as unbuildable and nothing is written", () => {
  const { requestPath } = releaseRequest(goldenReleaseInputs()["be-release"]);
  const outputDirectory = join(temporaryDirectory(), "trees");

  const run = runWorker(requestPath, outputDirectory, { CALIBRATION_TREE_MAX_RAW_BYTES: "100" });

  expect(run.exitCode).toBe(EXIT_UNBUILDABLE_INPUT);
  expect(run.stderr).toContain("exceeded CALIBRATION_TREE_MAX_RAW_BYTES");
  expect(writtenFiles(outputDirectory)).toEqual([]);
});

test("a request the worker cannot follow is a request error, not unbuildable input", () => {
  const release = goldenReleaseInputs()["be-release"];
  const cases: Array<[string, Record<string, unknown>, string]> = [
    ["an unknown kind", { kind: "staging" }, "kind must be \"release\""],
    ["an unregistered country", { country: "zz" }, "country"],
    ["a commit that is not a full SHA", { hfCommitSha: "main" }, "hfCommitSha"],
    ["an unknown field", { buildArtifactId: "a".repeat(64) }, "buildArtifactId"],
  ];
  for (const [description, overrides, message] of cases) {
    const { requestPath } = releaseRequest(release, overrides);
    const outputDirectory = join(temporaryDirectory(), "trees");

    const run = runWorker(requestPath, outputDirectory);

    expect({ description, exitCode: run.exitCode }).toEqual({ description, exitCode: EXIT_REQUEST_ERROR });
    expect(run.stderr).toContain(message);
    expect(writtenFiles(outputDirectory)).toEqual([]);
  }
});

test("a missing input file is a request error", () => {
  const { requestPath, inputDirectory } = releaseRequest(goldenReleaseInputs()["be-release"]);
  rmSync(join(inputDirectory, "calibration_diagnostics.json"));
  const outputDirectory = join(temporaryDirectory(), "trees");

  const run = runWorker(requestPath, outputDirectory);

  expect(run.exitCode).toBe(EXIT_REQUEST_ERROR);
  expect(writtenFiles(outputDirectory)).toEqual([]);
});

test("the worker refuses an output directory that already holds files", () => {
  const { requestPath } = releaseRequest(goldenReleaseInputs()["be-release"]);
  const outputDirectory = join(temporaryDirectory(), "trees");
  mkdirSync(outputDirectory);
  writeFileSync(join(outputDirectory, "earlier.json.gz"), "earlier");

  const run = runWorker(requestPath, outputDirectory);

  expect(run.exitCode).toBe(EXIT_REQUEST_ERROR);
  expect(run.stderr).toContain("must be empty");
  expect(writtenFiles(outputDirectory)).toEqual(["earlier.json.gz"]);
});

test("a byte-order mark and the file's digest are read as the publisher reads Hugging Face text", () => {
  const release = goldenReleaseInputs()["be-release"];
  const { requestPath, inputDirectory } = releaseRequest(release);
  const diagnosticsPath = join(inputDirectory, "calibration_diagnostics.json");
  writeFileSync(diagnosticsPath, Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from(release.diagnosticsText)]));
  const outputDirectory = join(temporaryDirectory(), "trees");

  const run = runWorker(requestPath, outputDirectory);

  // Response.text() drops a leading byte-order mark, so the publisher hashes
  // the text without it and builds the recorded tree.
  expect({ exitCode: run.exitCode, stderr: run.stderr }).toEqual({ exitCode: 0, stderr: "" });
  expect((JSON.parse(run.stdout) as WorkerSummary).buildArtifactId)
    .toBe(recorded.sources["be-release"].buildArtifactId);
});
