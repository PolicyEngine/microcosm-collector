// The calibration tree worker: builds one release's or staged run's tree
// bundle from local copies of a build's files, and writes each part's gzip
// bytes under an output directory, at the path the publisher uploads it to.
// The collector runs it as its own Bun process:
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
//     "createdAt": "<ISO 8601 publication time with Z or an offset>" or null,
//     "artifacts": {
//       "calibrationDiagnostics": {"path": "<path in the dataset>", "file": "<local file>"},
//       "buildManifest": {...} or null,
//       "releaseManifest": {...} or null,
//       "demographics": {...} or null
//     }
//   }
//
// or a staged run's, built from a staged bundle's submitted files:
//
//   {
//     "kind": "staging",
//     "country": "uk",
//     "runId": "<the staged run>",
//     "label": "<the candidate release, or the run id when it names none>",
//     "hfRepo": ..., "hfCommitSha": ..., "createdAt": ..., "artifacts": {...}
//   }
//
// A release bundle is built exactly as the publisher's `publishRelease` builds
// it from the same files, commit and publication time, so both write the same
// bytes: the golden tests hold each to the recorded trees. A staged run's is
// built as the publisher's `publishStagingSource` builds a staging tree, but
// from the files given here rather than the staging repo's run files. Like the
// publisher, it records the publication time as Date.prototype.toISOString()
// writes it. On success it prints one JSON line describing the written parts,
// with the manifest entry the publisher would record for the build, and exits
// 0. It writes nothing unless the whole bundle builds. The other exit codes:
//
//   1  the request could not be followed: a malformed request, a missing
//      file, an output directory that is not empty;
//   2  the files cannot be built into a tree: the builder refused them, so
//      running again on the same files cannot help;
//   3  anything else: a size limit configured as something other than a
//      positive integer, or an unexpected error, such as a defect in the
//      builder, which may succeed once fixed.
//
// The split between 2 and 3 follows the error's class, so it is approximate:
// the builder refuses most bad content with plain Errors (2), but a malformed
// row can also end in a TypeError (3), and the bundle's checks of its own
// output throw plain Errors (2) that only a defect should cause. A non-zero
// exit can leave parts already written when a write fails, so each attempt
// needs a new output directory.
import { mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { createHash } from "node:crypto";

import { CALIBRATION_TREE_SCHEMA_VERSION, type CalibrationTreeSourceArtifact } from "./calibration-tree-artifact";
import { buildCalibrationTreeBundle, type CalibrationTreeBundle } from "./calibration-tree-bundle";
import type { CalibrationTreeManifestEntry } from "./calibration-tree-manifest";
import { isCountry, type MicrocosmCountry } from "./countries";
import { assertSafeReleaseId, buildCalibration, InvalidReleaseIdError } from "./latest-artifact";
import { CalibrationTreeArtifactSizeError, enforceConfiguredSizeLimits } from "./publish-calibration-tree";
import { isBuildFolderId, isReleaseDirectory } from "./release-record";
import { IncompatibleStagingDataError } from "./staging-contract";
import { UnsupportedCalibrationDiagnosticsSchemaError } from "./target-representation";

type JsonObject = Record<string, unknown>;

export const CALIBRATION_TREE_WORKER_EXIT_REQUEST_ERROR = 1;
export const CALIBRATION_TREE_WORKER_EXIT_UNBUILDABLE_INPUT = 2;
export const CALIBRATION_TREE_WORKER_EXIT_WORKER_ERROR = 3;

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
const STAGING_REQUEST_FIELDS = [
  "kind",
  "country",
  "runId",
  "label",
  "hfRepo",
  "hfCommitSha",
  "createdAt",
  "artifacts",
] as const;
const HF_COMMIT_RE = /^[0-9a-f]{40,64}$/;
// A date and time with seconds and an explicit offset, so the instant never
// depends on the machine's time zone.
const PUBLICATION_TIME_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;
// The size limits enforceConfiguredSizeLimits reads.
const SIZE_LIMIT_VARIABLES = ["CALIBRATION_TREE_MAX_RAW_BYTES", "CALIBRATION_TREE_MAX_GZIP_BYTES"] as const;
const HF_REPO_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,95}\/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$/;

export interface CalibrationTreeWorkerArtifact {
  /** The file's path in the Hugging Face dataset, recorded in the index. */
  path: string;
  /** Where the file's bytes are on disk. */
  file: string;
}

export interface CalibrationTreeWorkerArtifacts {
  calibrationDiagnostics: CalibrationTreeWorkerArtifact;
  buildManifest: CalibrationTreeWorkerArtifact | null;
  releaseManifest: CalibrationTreeWorkerArtifact | null;
  demographics: CalibrationTreeWorkerArtifact | null;
}

export interface CalibrationTreeWorkerReleaseRequest {
  kind: "release";
  country: MicrocosmCountry;
  releaseId: string;
  releaseDirectory: string;
  hfRepo: string;
  hfCommitSha: string;
  createdAt: string | null;
  artifacts: CalibrationTreeWorkerArtifacts;
}

/** A staged run, built from its staged bundle's files. */
export interface CalibrationTreeWorkerStagingRequest {
  kind: "staging";
  country: MicrocosmCountry;
  runId: string;
  /** The candidate release the run was staged for, or the run id. */
  label: string;
  hfRepo: string;
  hfCommitSha: string;
  createdAt: string | null;
  artifacts: CalibrationTreeWorkerArtifacts;
}

export type CalibrationTreeWorkerRequest =
  | CalibrationTreeWorkerReleaseRequest
  | CalibrationTreeWorkerStagingRequest;

export interface CalibrationTreeWorkerPart {
  part: string;
  path: string;
  sha256: string;
  rawBytes: number;
  gzipBytes: number;
}

export interface CalibrationTreeWorkerResult {
  country: MicrocosmCountry;
  /** The release id for a release; the label for a staged run. */
  releaseId: string;
  hfRepo: string;
  hfCommitSha: string;
  createdAt: string | null;
  buildArtifactId: string;
  index: Omit<CalibrationTreeWorkerPart, "part">;
  files: CalibrationTreeWorkerPart[];
  /** The entry the publisher would add to the manifest for this build. */
  manifestEntry: CalibrationTreeManifestEntry;
}

/** The request could not be followed; nothing about the files is known. */
export class CalibrationTreeWorkerRequestError extends Error {}

/** The files were read but cannot be built into a tree. */
export class CalibrationTreeUnbuildableInputError extends Error {}

/** The worker is configured in a way the publisher would also refuse. */
export class CalibrationTreeWorkerConfigurationError extends Error {}

/**
 * Whether an error from building the tree is the builder refusing the files.
 * The builder refuses content with plain Errors and its own error classes;
 * any other error, such as a TypeError, is treated as a defect, not as a
 * judgement about the files.
 */
export function isCalibrationTreeContentRefusal(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  return error.constructor === Error ||
    error instanceof CalibrationTreeArtifactSizeError ||
    error instanceof IncompatibleStagingDataError ||
    error instanceof UnsupportedCalibrationDiagnosticsSchemaError ||
    error instanceof InvalidReleaseIdError;
}

/** The exit code for an error that stopped the worker. */
export function calibrationTreeWorkerExitCode(error: unknown): number {
  if (error instanceof CalibrationTreeWorkerRequestError) return CALIBRATION_TREE_WORKER_EXIT_REQUEST_ERROR;
  if (error instanceof CalibrationTreeUnbuildableInputError) return CALIBRATION_TREE_WORKER_EXIT_UNBUILDABLE_INPUT;
  return CALIBRATION_TREE_WORKER_EXIT_WORKER_ERROR;
}

// enforceConfiguredSizeLimits refuses a limit that is not a positive integer
// with a plain Error, which would read as a refusal of the files, so the
// worker checks the same rule before it builds anything.
function checkConfiguredSizeLimits(): void {
  for (const name of SIZE_LIMIT_VARIABLES) {
    const configured = process.env[name]?.trim();
    if (!configured) continue;
    const limit = Number(configured);
    if (!Number.isSafeInteger(limit) || limit <= 0) {
      throw new CalibrationTreeWorkerConfigurationError(`${name} must be a positive integer.`);
    }
  }
}

// The publisher stores each publication time as toISOString() writes it.
function publicationTime(value: unknown): string | null {
  if (value === null) return null;
  const instant = typeof value === "string" && PUBLICATION_TIME_RE.test(value) ? Date.parse(value) : Number.NaN;
  if (!Number.isFinite(instant)) {
    throw new CalibrationTreeWorkerRequestError(
      "createdAt must be an ISO 8601 date and time with Z or a UTC offset, or null.",
    );
  }
  return new Date(instant).toISOString();
}

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

function stagedRunId(value: JsonObject): string {
  const runId = requestString(value, "runId");
  try {
    if (runId === "latest") throw new InvalidReleaseIdError("Invalid run id");
    return assertSafeReleaseId(runId, "run");
  } catch {
    throw new CalibrationTreeWorkerRequestError("runId must be a run id: letters, digits, dots, underscores and hyphens.");
  }
}

/** Checks a request read from JSON, refusing anything the worker would guess at. */
export function parseCalibrationTreeWorkerRequest(value: unknown): CalibrationTreeWorkerRequest {
  const request = requestObject(value, "The request");
  if (request.kind !== "release" && request.kind !== "staging") {
    throw new CalibrationTreeWorkerRequestError('kind must be "release" or "staging".');
  }
  onlyFields(request, request.kind === "release" ? REQUEST_FIELDS : STAGING_REQUEST_FIELDS, "The request");
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
  const createdAt = publicationTime(request.createdAt);
  const artifactFields = requestObject(request.artifacts, "artifacts");
  onlyFields(artifactFields, ARTIFACT_NAMES, "artifacts");
  for (const name of ARTIFACT_NAMES) {
    if (!(name in artifactFields)) {
      throw new CalibrationTreeWorkerRequestError(`artifacts.${name} is required (null when absent).`);
    }
  }
  const artifacts = {
    calibrationDiagnostics: workerArtifact(artifactFields.calibrationDiagnostics, "calibrationDiagnostics")!,
    buildManifest: workerArtifact(artifactFields.buildManifest, "buildManifest"),
    releaseManifest: workerArtifact(artifactFields.releaseManifest, "releaseManifest"),
    demographics: workerArtifact(artifactFields.demographics, "demographics"),
  };
  if (request.kind === "staging") {
    return {
      kind: "staging",
      country,
      runId: stagedRunId(request),
      label: requestString(request, "label"),
      hfRepo,
      hfCommitSha,
      createdAt,
      artifacts,
    };
  }
  return {
    kind: "release",
    country,
    releaseId: releaseFolder(request, "releaseId"),
    releaseDirectory: releaseFolder(request, "releaseDirectory"),
    hfRepo,
    hfCommitSha,
    createdAt,
    artifacts,
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

/**
 * Builds the requested bundle in memory: a release as the publisher's
 * publishRelease does, a staged run as its publishStagingSource does.
 */
export function buildCalibrationTreeWorkerBundle(request: CalibrationTreeWorkerRequest): CalibrationTreeBundle {
  const [diagnostics, buildManifest, releaseManifest, demographics] = ARTIFACT_NAMES.map((name) =>
    readArtifact(request.artifacts[name])
  );
  if (!diagnostics) throw new CalibrationTreeWorkerRequestError("Calibration diagnostics are missing.");
  try {
    const sourceArtifacts = {
      calibrationDiagnostics: diagnostics.source,
      buildManifest: buildManifest?.source ?? null,
      releaseManifest: releaseManifest?.source ?? null,
      demographics: demographics?.source ?? null,
    };
    if (request.kind === "staging") {
      return buildStagingBundle(request, sourceArtifacts, {
        diagnostics: diagnostics.json,
        buildManifest: buildManifest?.json ?? {},
        releaseManifest: releaseManifest?.json ?? {},
        demographics: demographics?.json ?? {},
      });
    }
    return buildReleaseBundle(request, sourceArtifacts, {
      diagnostics: diagnostics.json,
      buildManifest: buildManifest?.json ?? {},
      releaseManifest: releaseManifest?.json ?? {},
      demographics: demographics?.json ?? {},
    });
  } catch (error) {
    if (!isCalibrationTreeContentRefusal(error)) throw error;
    throw new CalibrationTreeUnbuildableInputError((error as Error).message);
  }
}

interface WorkerDocuments {
  diagnostics: JsonObject;
  buildManifest: JsonObject;
  releaseManifest: JsonObject;
  demographics: JsonObject;
}

type WorkerSourceArtifacts = Parameters<typeof buildCalibrationTreeBundle>[0]["sourceArtifacts"];

// As publishStagingSource builds a new staged run's tree: a staging build of
// the run, labelled and released under its candidate release, comparing
// under the release its diagnostics name.
function buildStagingBundle(
  request: CalibrationTreeWorkerStagingRequest,
  sourceArtifacts: WorkerSourceArtifacts,
  documents: WorkerDocuments,
): CalibrationTreeBundle {
  const { country, runId, label, hfRepo, hfCommitSha, createdAt } = request;
  const calibration = buildCalibration(
    documents.diagnostics,
    label,
    createdAt,
    documents.buildManifest,
    documents.releaseManifest,
    documents.demographics,
    country,
    "huggingface_staged_bundle",
    sourceArtifacts.calibrationDiagnostics?.sha256 ?? null,
    hfCommitSha,
  );
  const bundle = buildCalibrationTreeBundle({
    country,
    buildKind: "staging",
    sourceId: runId,
    label,
    createdAt,
    releaseId: label,
    hfRepo,
    hfCommitSha,
    sourceArtifacts,
    rows: calibration.rows,
    calibrationProvenance: calibration.calibration_provenance,
    lossAttributionAvailable: calibration.target_loss_attribution.status !== "unavailable",
    comparison: {
      releaseId: calibration.release_id,
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
}

function buildReleaseBundle(
  request: CalibrationTreeWorkerReleaseRequest,
  sourceArtifacts: WorkerSourceArtifacts,
  documents: WorkerDocuments,
): CalibrationTreeBundle {
  const { country, releaseId: id, releaseDirectory, hfCommitSha } = request;
  const calibration = buildCalibration(
    documents.diagnostics,
    id,
    null,
    documents.buildManifest,
    documents.releaseManifest,
    documents.demographics,
    country,
    "huggingface_immutable",
    sourceArtifacts.calibrationDiagnostics?.sha256 ?? null,
    hfCommitSha,
  );
  const bundle = buildCalibrationTreeBundle({
    country,
    createdAt: request.createdAt,
    releaseId: id,
    hfRepo: request.hfRepo,
    hfCommitSha,
    sourceArtifacts,
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
}

// The entry the publisher adds to the manifest for the build: releaseManifestEntry
// for a release, publishStagingSource's new entry for a staged run.
function manifestEntry(
  request: CalibrationTreeWorkerRequest,
  bundle: CalibrationTreeBundle,
  index: Omit<CalibrationTreeWorkerPart, "part">,
): CalibrationTreeManifestEntry {
  const build = bundle.index.build;
  const shared = {
    buildArtifactId: build.buildArtifactId,
    hfRepo: request.hfRepo,
    hfCommitSha: request.hfCommitSha,
    treeSchemaVersion: CALIBRATION_TREE_SCHEMA_VERSION,
    indexSha256: index.sha256,
    indexBytes: index.rawBytes,
    createdAt: build.createdAt,
    updatedAt: build.createdAt,
  };
  if (request.kind === "staging") {
    return {
      ...shared,
      kind: "staging",
      sourceId: request.runId,
      label: request.label,
      releaseId: null,
      stagingRunId: request.runId,
    };
  }
  return {
    ...shared,
    kind: "release",
    sourceId: build.sourceId,
    label: build.label,
    releaseId: request.releaseId,
    // Recorded only for a tag read from its release's folder.
    ...(request.releaseDirectory !== request.releaseId ? { releaseDirectory: request.releaseDirectory } : {}),
    stagingRunId: null,
  };
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
  checkConfiguredSizeLimits();
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
    releaseId: request.kind === "staging" ? request.label : request.releaseId,
    hfRepo: request.hfRepo,
    hfCommitSha: request.hfCommitSha,
    createdAt: request.createdAt,
    buildArtifactId: bundle.index.buildArtifactId,
    index,
    files,
    manifestEntry: manifestEntry(request, bundle, index),
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
    return calibrationTreeWorkerExitCode(error);
  }
}

if (import.meta.main) {
  process.exitCode = calibrationTreeWorkerMain(Bun.argv.slice(2));
}
