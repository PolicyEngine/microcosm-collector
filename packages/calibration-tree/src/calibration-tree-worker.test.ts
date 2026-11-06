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
import { InvalidReleaseIdError, microcosmRepo } from "@policyengine/calibration-tree/latest-artifact";
import * as calibrationTreeWorker from "./calibration-tree-worker";
import { CountryNotConfiguredError } from "./countries";
import { CalibrationTreeArtifactSizeError } from "./publish-calibration-tree";
import { IncompatibleStagingDataError } from "./staging-contract";
import { UnsupportedCalibrationDiagnosticsSchemaError } from "./target-representation";
import {
  GZIP_HEADER_WITHOUT_OS,
  goldenReleaseInputs,
  goldenSourceBuild,
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
const EXIT_WORKER_ERROR = 3;

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
  manifestEntry?: Record<string, unknown>;
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
  build: Record<string, unknown> & { releaseId: string };
  targetComparison: { releaseId: string };
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
  expect(index.targetComparison.releaseId).toBe(buildFolder);
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
  expect(index.targetComparison.releaseId).toBe(tag);
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
  expect(index.targetComparison.releaseId).toBe(release.releaseId);
});

test(
  "the manifest entry for a recorded release is the one the publisher records",
  async () => {
    const { requestPath } = releaseRequest(goldenReleaseInputs()["uk-release"]);
    const outputDirectory = join(temporaryDirectory(), "trees");

    const run = runWorker(requestPath, outputDirectory);

    expect({ exitCode: run.exitCode, stderr: run.stderr }).toEqual({ exitCode: 0, stderr: "" });
    const summary = JSON.parse(run.stdout) as WorkerSummary;
    expect(summary.manifestEntry).toEqual({ ...(await goldenSourceBuild("uk-release")).manifestEntry });
  },
  BUILD_TIMEOUT_MS,
);

test("a tag read from its release's folder records the folder in its manifest entry", () => {
  const release = goldenReleaseInputs()["uk-release"];
  const tag = `${release.releaseId}-20261004`;
  const { requestPath } = releaseRequest(release, { releaseId: tag, releaseDirectory: release.releaseId });
  const outputDirectory = join(temporaryDirectory(), "trees");

  const run = runWorker(requestPath, outputDirectory);

  expect({ exitCode: run.exitCode, stderr: run.stderr }).toEqual({ exitCode: 0, stderr: "" });
  const summary = JSON.parse(run.stdout) as WorkerSummary;
  expect(summary.manifestEntry).toMatchObject({
    kind: "release",
    releaseId: tag,
    releaseDirectory: release.releaseId,
    stagingRunId: null,
  });
});

const STAGED_RUN_ID = "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71";
const STAGED_LABEL = "microcosm-uk-2024-25-national";
const STAGED_ACCEPTED_AT = "2026-10-19T09:41:07.000Z";

// Writes a recorded release's diagnostics where a staged bundle's submitted
// files would be downloaded, and a staging request naming them.
function stagingRequest(overrides: Record<string, unknown> = {}): { requestPath: string } {
  const release = goldenReleaseInputs()["uk-release"];
  const inputDirectory = temporaryDirectory();
  writeFileSync(join(inputDirectory, "calibration_diagnostics.json"), release.diagnosticsText);
  const request = {
    kind: "staging",
    country: "uk",
    runId: STAGED_RUN_ID,
    label: STAGED_LABEL,
    hfRepo: microcosmRepo("uk"),
    hfCommitSha: release.hfCommitSha,
    createdAt: STAGED_ACCEPTED_AT,
    artifacts: {
      calibrationDiagnostics: {
        path: `staged/${STAGED_RUN_ID}/calibration_diagnostics.json`,
        file: join(inputDirectory, "calibration_diagnostics.json"),
      },
      buildManifest: null,
      releaseManifest: null,
      demographics: null,
    },
    ...overrides,
  };
  const requestPath = join(inputDirectory, "request.json");
  writeFileSync(requestPath, JSON.stringify(request));
  return { requestPath };
}

test("a staged bundle's tree is a staging build of its run, with the requested label and time", () => {
  const release = goldenReleaseInputs()["uk-release"];
  const { requestPath } = stagingRequest();
  const outputDirectory = join(temporaryDirectory(), "trees");

  const run = runWorker(requestPath, outputDirectory);

  expect({ exitCode: run.exitCode, stderr: run.stderr }).toEqual({ exitCode: 0, stderr: "" });
  const summary = JSON.parse(run.stdout) as WorkerSummary;
  const index = writtenIndex(outputDirectory, summary);
  expect(index.build).toMatchObject({
    buildArtifactId: summary.buildArtifactId,
    kind: "staging",
    sourceId: STAGED_RUN_ID,
    label: STAGED_LABEL,
    releaseId: STAGED_LABEL,
    hfRepo: microcosmRepo("uk"),
    hfCommitSha: release.hfCommitSha,
    createdAt: STAGED_ACCEPTED_AT,
  });
  // As the publisher's staging builds do, it compares under the release its
  // diagnostics name.
  expect(index.targetComparison.releaseId).toBe(release.releaseId);
  expect(summary.manifestEntry).toEqual({
    buildArtifactId: summary.buildArtifactId,
    kind: "staging",
    sourceId: STAGED_RUN_ID,
    label: STAGED_LABEL,
    releaseId: null,
    stagingRunId: STAGED_RUN_ID,
    hfRepo: microcosmRepo("uk"),
    hfCommitSha: release.hfCommitSha,
    treeSchemaVersion: 6,
    indexSha256: summary.index.sha256,
    indexBytes: summary.index.rawBytes,
    createdAt: STAGED_ACCEPTED_AT,
    updatedAt: STAGED_ACCEPTED_AT,
  });
});

test("the same staging request writes the same tree again, so reprocessing changes nothing", () => {
  const { requestPath } = stagingRequest();
  const firstDirectory = join(temporaryDirectory(), "trees");
  const secondDirectory = join(temporaryDirectory(), "trees");

  const first = runWorker(requestPath, firstDirectory);
  const second = runWorker(requestPath, secondDirectory);

  expect([first.exitCode, second.exitCode]).toEqual([0, 0]);
  expect(second.stdout).toBe(first.stdout);
  const firstSummary = JSON.parse(first.stdout) as WorkerSummary;
  for (const file of firstSummary.files) {
    expect(sha256(readFileSync(join(secondDirectory, file.path)))).toBe(
      sha256(readFileSync(join(firstDirectory, file.path))),
    );
  }
});

test("a staging request names its run and label and takes no release fields", () => {
  const accepted = stagingRequest();
  expect(runWorker(accepted.requestPath, join(temporaryDirectory(), "trees")).exitCode).toBe(0);
  const cases: Array<Record<string, unknown>> = [
    { runId: "" },
    { runId: "../uk-run" },
    { label: "" },
    { releaseId: STAGED_LABEL },
    { releaseDirectory: STAGED_LABEL },
  ];
  for (const overrides of cases) {
    const { requestPath } = stagingRequest(overrides);
    const outputDirectory = join(temporaryDirectory(), "trees");

    const run = runWorker(requestPath, outputDirectory);

    expect({ overrides, exitCode: run.exitCode }).toEqual({ overrides, exitCode: EXIT_REQUEST_ERROR });
    expect(writtenFiles(outputDirectory)).toEqual([]);
  }
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
    ["an unknown kind", { kind: "comparison" }, "kind must be \"release\" or \"staging\""],
    ["an unregistered country", { country: "xx" }, "country xx"],
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

test("a publication time with an offset is recorded as the publisher records it", () => {
  const release = goldenReleaseInputs()["be-release"];
  const expected = recorded.sources["be-release"];
  expect(release.publishedAt).toBe("2026-09-16T12:00:00.000Z");
  // The dataset's releases record writes times like the first; the publisher
  // stores every publication time as Date.prototype.toISOString() writes it.
  for (const createdAt of ["2026-09-16T12:00:00+00:00", "2026-09-16T14:00:00.000+02:00"]) {
    const { requestPath } = releaseRequest(release, { createdAt });
    const outputDirectory = join(temporaryDirectory(), "trees");

    const run = runWorker(requestPath, outputDirectory);

    expect({ createdAt, exitCode: run.exitCode, stderr: run.stderr }).toEqual({ createdAt, exitCode: 0, stderr: "" });
    const summary = JSON.parse(run.stdout) as WorkerSummary & { createdAt: string };
    expect(summary.createdAt).toBe(release.publishedAt);
    expect(summary.files.map((file) => writtenFileRecord(outputDirectory, file.part, file.path)))
      .toEqual(expected.files);
  }
});

test("a publication time without a UTC offset is a request error", () => {
  const release = goldenReleaseInputs()["be-release"];
  // Each parses as a date, but the first two would be read in the machine's
  // own time zone.
  for (const createdAt of ["2026-09-16T12:00:00", "September 16, 2026 12:00", "2026-09-16"]) {
    const { requestPath } = releaseRequest(release, { createdAt });
    const outputDirectory = join(temporaryDirectory(), "trees");

    const run = runWorker(requestPath, outputDirectory);

    expect({ createdAt, exitCode: run.exitCode }).toEqual({ createdAt, exitCode: EXIT_REQUEST_ERROR });
    expect(run.stderr).toContain("createdAt");
    expect(writtenFiles(outputDirectory)).toEqual([]);
  }
});

test("a size limit that is not a positive integer is a worker error, not unbuildable input", () => {
  for (const [name, value] of [
    ["CALIBRATION_TREE_MAX_RAW_BYTES", "100MB"],
    ["CALIBRATION_TREE_MAX_GZIP_BYTES", "0"],
  ]) {
    const { requestPath } = releaseRequest(goldenReleaseInputs()["be-release"]);
    const outputDirectory = join(temporaryDirectory(), "trees");

    const run = runWorker(requestPath, outputDirectory, { [name]: value });

    expect({ name, exitCode: run.exitCode }).toEqual({ name, exitCode: EXIT_WORKER_ERROR });
    expect(run.stderr).toContain(`${name} must be a positive integer.`);
    expect(writtenFiles(outputDirectory)).toEqual([]);
  }
});

test("only the builder's refusals of the files count as unbuildable input", () => {
  const isContentRefusal = calibrationTreeWorker.isCalibrationTreeContentRefusal;
  expect(typeof isContentRefusal).toBe("function");

  const refusals = [
    new Error("Calibration diagnostics have no targets."),
    new CalibrationTreeArtifactSizeError("too large"),
    new IncompatibleStagingDataError("incompatible"),
    new UnsupportedCalibrationDiagnosticsSchemaError("unsupported"),
    new InvalidReleaseIdError("Invalid release id"),
  ];
  const failures = [
    new TypeError("Cannot read properties of undefined"),
    new RangeError("Invalid string length"),
    new ReferenceError("undefinedName is not defined"),
    new CountryNotConfiguredError("zz"),
    "a thrown string",
  ];
  expect(refusals.map((error) => isContentRefusal(error))).toEqual(refusals.map(() => true));
  expect(failures.map((error) => isContentRefusal(error))).toEqual(failures.map(() => false));
});

test("each kind of failure has its own exit code", () => {
  const exitCodeFor = calibrationTreeWorker.calibrationTreeWorkerExitCode;
  expect(typeof exitCodeFor).toBe("function");

  expect(exitCodeFor(new calibrationTreeWorker.CalibrationTreeWorkerRequestError("request")))
    .toBe(EXIT_REQUEST_ERROR);
  expect(exitCodeFor(new calibrationTreeWorker.CalibrationTreeUnbuildableInputError("content")))
    .toBe(EXIT_UNBUILDABLE_INPUT);
  for (const error of [new Error("disk full"), new TypeError("defect"), "a thrown string"]) {
    expect(exitCodeFor(error)).toBe(EXIT_WORKER_ERROR);
  }
});
