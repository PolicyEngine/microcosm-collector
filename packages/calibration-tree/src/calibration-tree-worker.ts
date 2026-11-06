// The calibration tree worker: builds one release's tree bundle from local
// copies of the files the publisher reads from Hugging Face, and writes each
// part's gzip bytes under an output directory, at the path the publisher
// uploads it to. The collector runs it as its own Bun process:
//
//   bun run src/calibration-tree-worker.ts REQUEST.json OUTPUT_DIRECTORY
//
// REQUEST.json names the release, where its files came from and where they
// are on disk:
//
//   {
//     "kind": "release",
//     "country": "uk",
//     "releaseId": "<release, tag or <release>/<build> folder>",
//     "releaseDirectory": "<folder under releases/ the files were read from>",
//     "hfRepo": "<owner>/<dataset>",
//     "hfCommitSha": "<full commit SHA>",
//     "createdAt": "<ISO 8601 publication time>" or null,
//     "artifacts": {
//       "calibrationDiagnostics": {"path": "<path in the dataset>", "file": "<local file>"},
//       "buildManifest": {...} or null,
//       "releaseManifest": {...} or null,
//       "demographics": {...} or null
//     }
//   }
//
// It builds the bundle exactly as the publisher's `publishRelease` does from
// the same files, commit and publication time, so both write the same bytes:
// the golden tests hold each to the recorded trees. On success it prints one
// JSON line describing the written parts and exits 0. It writes nothing
// unless the whole bundle builds. Exit code 2 means the files cannot be
// built into a tree (the content is refused); 1 means the request could not
// be followed (a malformed request, a missing file, an output directory that
// is not empty).
import { mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { createHash } from "node:crypto";

import type { CalibrationTreeSourceArtifact } from "./calibration-tree-artifact";
import { buildCalibrationTreeBundle, type CalibrationTreeBundle } from "./calibration-tree-bundle";
import { isCountry, type MicrocosmCountry } from "./countries";
import { buildCalibration } from "./latest-artifact";
import { enforceConfiguredSizeLimits } from "./publish-calibration-tree";
import { isBuildFolderId, isReleaseDirectory } from "./release-record";

type JsonObject = Record<string, unknown>;

export const CALIBRATION_TREE_WORKER_EXIT_REQUEST_ERROR = 1;
export const CALIBRATION_TREE_WORKER_EXIT_UNBUILDABLE_INPUT = 2;

const ARTIFACT_NAMES = ["calibrationDiagnostics", "buildManifest", "releaseManifest", "demographics"] as const;
type ArtifactName = (typeof ARTIFACT_NAMES)[number];
const REQUEST_FIELDS = [
  "kind",
  "country",
  "releaseId",
  "releaseDirectory",
  "hfRepo",
  "hfCommitSha",
  "createdAt",
  "artifacts",
] as const;
const HF_COMMIT_RE = /^[0-9a-f]{40,64}$/;
const HF_REPO_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,95}\/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$/;

export interface CalibrationTreeWorkerArtifact {
  /** The file's path in the Hugging Face dataset, recorded in the index. */
  path: string;
  /** Where the file's bytes are on disk. */
  file: string;
}

export interface CalibrationTreeWorkerRequest {
  kind: "release";
  country: MicrocosmCountry;
  releaseId: string;
  releaseDirectory: string;
  hfRepo: string;
  hfCommitSha: string;
  createdAt: string | null;
  artifacts: {
    calibrationDiagnostics: CalibrationTreeWorkerArtifact;
    buildManifest: CalibrationTreeWorkerArtifact | null;
    releaseManifest: CalibrationTreeWorkerArtifact | null;
    demographics: CalibrationTreeWorkerArtifact | null;
  };
}

export interface CalibrationTreeWorkerPart {
  part: string;
  path: string;
  sha256: string;
  rawBytes: number;
  gzipBytes: number;
}

export interface CalibrationTreeWorkerResult {
  country: MicrocosmCountry;
  releaseId: string;
  hfRepo: string;
  hfCommitSha: string;
  createdAt: string | null;
  buildArtifactId: string;
  index: Omit<CalibrationTreeWorkerPart, "part">;
  files: CalibrationTreeWorkerPart[];
}

/** The request could not be followed; nothing about the files is known. */
export class CalibrationTreeWorkerRequestError extends Error {}

/** The files were read but cannot be built into a tree. */
export class CalibrationTreeUnbuildableInputError extends Error {}

function requestObject(value: unknown, label: string): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new CalibrationTreeWorkerRequestError(`${label} must be a JSON object.`);
  }
  return value as JsonObject;
}

function onlyFields(value: JsonObject, fields: readonly string[], label: string): void {
  const unknown = Object.keys(value).filter((field) => !fields.includes(field));
  if (unknown.length > 0) {
    throw new CalibrationTreeWorkerRequestError(`${label} has unknown fields: ${unknown.join(", ")}.`);
  }
}

function requestString(value: JsonObject, field: string, label = field): string {
  const text = value[field];
  if (typeof text !== "string" || text.length === 0) {
    throw new CalibrationTreeWorkerRequestError(`${label} must be a non-empty string.`);
  }
  return text;
}

function releaseFolder(value: JsonObject, field: string): string {
  const text = requestString(value, field);
  // The publisher refuses the same values (releaseOrBuildFolderId).
  if (!isReleaseDirectory(text) || text === "latest") {
    throw new CalibrationTreeWorkerRequestError(`${field} must be a release id or a <release>/<build> folder.`);
  }
  return text;
}

function workerArtifact(value: unknown, name: ArtifactName): CalibrationTreeWorkerArtifact | null {
  if (value === null && name !== "calibrationDiagnostics") return null;
  const artifact = requestObject(value, `artifacts.${name}`);
  onlyFields(artifact, ["path", "file"], `artifacts.${name}`);
  return {
    path: requestString(artifact, "path", `artifacts.${name}.path`),
    file: requestString(artifact, "file", `artifacts.${name}.file`),
  };
}

/** Checks a request read from JSON, refusing anything the worker would guess at. */
export function parseCalibrationTreeWorkerRequest(value: unknown): CalibrationTreeWorkerRequest {
  const request = requestObject(value, "The request");
  onlyFields(request, REQUEST_FIELDS, "The request");
  if (request.kind !== "release") {
    throw new CalibrationTreeWorkerRequestError('kind must be "release".');
  }
  const country = requestString(request, "country");
  if (!isCountry(country)) {
    throw new CalibrationTreeWorkerRequestError(`country ${country} is not a registered country.`);
  }
  const hfRepo = requestString(request, "hfRepo");
  if (!HF_REPO_RE.test(hfRepo)) {
    throw new CalibrationTreeWorkerRequestError("hfRepo must be an <owner>/<dataset> repository.");
  }
  const hfCommitSha = requestString(request, "hfCommitSha");
  if (!HF_COMMIT_RE.test(hfCommitSha)) {
    throw new CalibrationTreeWorkerRequestError("hfCommitSha must be a full lowercase commit SHA.");
  }
  const createdAt = request.createdAt;
  if (createdAt !== null && (typeof createdAt !== "string" || Number.isNaN(Date.parse(createdAt)))) {
    throw new CalibrationTreeWorkerRequestError("createdAt must be an ISO 8601 time or null.");
  }
  const artifacts = requestObject(request.artifacts, "artifacts");
  onlyFields(artifacts, ARTIFACT_NAMES, "artifacts");
  for (const name of ARTIFACT_NAMES) {
    if (!(name in artifacts)) {
      throw new CalibrationTreeWorkerRequestError(`artifacts.${name} is required (null when absent).`);
    }
  }
  return {
    kind: "release",
    country,
    releaseId: releaseFolder(request, "releaseId"),
    releaseDirectory: releaseFolder(request, "releaseDirectory"),
    hfRepo,
    hfCommitSha,
    createdAt,
    artifacts: {
      calibrationDiagnostics: workerArtifact(artifacts.calibrationDiagnostics, "calibrationDiagnostics")!,
      buildManifest: workerArtifact(artifacts.buildManifest, "buildManifest"),
      releaseManifest: workerArtifact(artifacts.releaseManifest, "releaseManifest"),
      demographics: workerArtifact(artifacts.demographics, "demographics"),
    },
  };
}

interface ReadArtifact {
  json: JsonObject;
  source: CalibrationTreeSourceArtifact;
}

// The publisher hashes and parses Response.text(): UTF-8, with a leading
// byte-order mark dropped and invalid bytes replaced. TextDecoder's defaults
// decode the same way, so a file gives the publisher's digest and document.
function readArtifact(artifact: CalibrationTreeWorkerArtifact | null): ReadArtifact | null {
  if (artifact == null) return null;
  let bytes: Uint8Array;
  try {
    bytes = readFileSync(artifact.file);
  } catch (error) {
    throw new CalibrationTreeWorkerRequestError(
      `${artifact.path} could not be read from ${artifact.file}: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  const text = new TextDecoder("utf-8").decode(bytes);
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new CalibrationTreeUnbuildableInputError(`${artifact.path} is not valid JSON.`);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new CalibrationTreeUnbuildableInputError(`${artifact.path} must be a JSON object.`);
  }
  return {
    json: parsed as JsonObject,
    source: { path: artifact.path, sha256: createHash("sha256").update(text, "utf8").digest("hex") },
  };
}

/** Builds the release's bundle in memory, as the publisher's publishRelease does. */
export function buildCalibrationTreeWorkerBundle(request: CalibrationTreeWorkerRequest): CalibrationTreeBundle {
  const [diagnostics, buildManifest, releaseManifest, demographics] = ARTIFACT_NAMES.map((name) =>
    readArtifact(request.artifacts[name])
  );
  if (!diagnostics) throw new CalibrationTreeWorkerRequestError("Calibration diagnostics are missing.");
  const { country, releaseId: id, releaseDirectory, hfCommitSha } = request;
  try {
    const calibration = buildCalibration(
      diagnostics.json,
      id,
      null,
      buildManifest?.json ?? {},
      releaseManifest?.json ?? {},
      demographics?.json ?? {},
      country,
      "huggingface_immutable",
      diagnostics.source.sha256,
      hfCommitSha,
    );
    const bundle = buildCalibrationTreeBundle({
      country,
      createdAt: request.createdAt,
      releaseId: id,
      hfRepo: request.hfRepo,
      hfCommitSha,
      sourceArtifacts: {
        calibrationDiagnostics: diagnostics.source,
        buildManifest: buildManifest?.source ?? null,
        releaseManifest: releaseManifest?.source ?? null,
        demographics: demographics?.source ?? null,
      },
      rows: calibration.rows,
      calibrationProvenance: calibration.calibration_provenance,
      lossAttributionAvailable: calibration.target_loss_attribution.status !== "unavailable",
      comparison: {
        // As in publishRelease: a tag or a build folder compares under its
        // own id, a release folder under the release its diagnostics name.
        releaseId: releaseDirectory === id && !isBuildFolderId(id) ? calibration.release_id : id,
        calibrationProvenance: calibration.calibration_provenance,
        status: calibration.target_loss_attribution.status,
        aggregate: calibration.target_loss_attribution.aggregate,
        cap: calibration.target_loss_attribution.cap,
        basisIdentifier: calibration.target_loss_attribution.basis_identifier,
        targetRepresentation: calibration.target_schema.target_representation,
      },
    });
    for (const file of bundle.files) enforceConfiguredSizeLimits(file);
    return bundle;
  } catch (error) {
    if (error instanceof CalibrationTreeWorkerRequestError) throw error;
    throw new CalibrationTreeUnbuildableInputError(error instanceof Error ? error.message : String(error));
  }
}

function emptyOutputDirectory(outputDirectory: string): void {
  try {
    mkdirSync(outputDirectory, { recursive: true });
    if (readdirSync(outputDirectory).length > 0) {
      throw new CalibrationTreeWorkerRequestError(`The output directory ${outputDirectory} must be empty.`);
    }
  } catch (error) {
    if (error instanceof CalibrationTreeWorkerRequestError) throw error;
    throw new CalibrationTreeWorkerRequestError(
      `The output directory ${outputDirectory} cannot be used: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
}

/** Builds the requested tree and writes its parts; returns what was written. */
export function runCalibrationTreeWorker(
  request: CalibrationTreeWorkerRequest,
  outputDirectory: string,
): CalibrationTreeWorkerResult {
  emptyOutputDirectory(outputDirectory);
  const bundle = buildCalibrationTreeWorkerBundle(request);
  for (const file of bundle.files) {
    const destination = join(outputDirectory, file.path);
    mkdirSync(dirname(destination), { recursive: true });
    writeFileSync(destination, file.compressed, { flag: "wx" });
  }
  const files = bundle.files.map((file) => ({
    part: file.part,
    path: file.path,
    sha256: file.sha256,
    rawBytes: file.rawBytes,
    gzipBytes: file.gzipBytes,
  }));
  const { part: _index, ...index } = files.find((file) => file.part === "index")!;
  return {
    country: request.country,
    releaseId: request.releaseId,
    hfRepo: request.hfRepo,
    hfCommitSha: request.hfCommitSha,
    createdAt: request.createdAt,
    buildArtifactId: bundle.index.buildArtifactId,
    index,
    files,
  };
}

function readRequestFile(requestPath: string): CalibrationTreeWorkerRequest {
  let text: string;
  try {
    text = readFileSync(requestPath, "utf8");
  } catch (error) {
    throw new CalibrationTreeWorkerRequestError(
      `The request ${requestPath} could not be read: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new CalibrationTreeWorkerRequestError(`The request ${requestPath} is not valid JSON.`);
  }
  return parseCalibrationTreeWorkerRequest(parsed);
}

/** The worker's command line: returns the process exit code. */
export function calibrationTreeWorkerMain(argv: string[]): number {
  try {
    if (argv.length !== 2) {
      throw new CalibrationTreeWorkerRequestError(
        "Usage: bun run src/calibration-tree-worker.ts REQUEST.json OUTPUT_DIRECTORY",
      );
    }
    const [requestPath, outputDirectory] = argv;
    const result = runCalibrationTreeWorker(readRequestFile(requestPath), outputDirectory);
    console.log(JSON.stringify(result));
    return 0;
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    return error instanceof CalibrationTreeUnbuildableInputError
      ? CALIBRATION_TREE_WORKER_EXIT_UNBUILDABLE_INPUT
      : CALIBRATION_TREE_WORKER_EXIT_REQUEST_ERROR;
  }
}

if (import.meta.main) {
  process.exitCode = calibrationTreeWorkerMain(Bun.argv.slice(2));
}
