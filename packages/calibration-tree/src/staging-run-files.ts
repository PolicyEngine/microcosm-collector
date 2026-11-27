// The stored copies of staging run files. The collector's webhook-queued job
// and backfill copy the files the dashboard reads for the runs in a country's
// Hugging Face staging repository to private blobs:
//
//   staging-run-files/<country>/index.json
//   staging-run-files/<country>/runs/<run ID>/<SHA-256 of the file>.gz
//
// The index is the one document that says which copies are current, so a
// reader never sees part of an update. A file is stored under its own digest
// and never overwritten with other bytes. The folder is apart from
// calibration-trees/, so nothing that lists or reads the trees sees it. The
// copies hold what the run's files say. A run's current status for an
// unfinished run comes from telemetry when a page is requested.
import { get } from "@vercel/blob";
import { createHash } from "node:crypto";
import { gunzipSync } from "node:zlib";

import { isSha256 } from "./calibration-tree-artifact";
import type { CalibrationTreeBlobClient } from "./calibration-tree-blob";
import type { MicrocosmCountry } from "./countries";
import {
  sortRuns,
  type IncompatibleStagingRun,
  type StagingRunSummary,
} from "./staging-artifact";
import {
  isStructuredStagingDocument,
  parseStagingCalibrationProgress,
  parseStagingEvents,
  parseStagingManifest,
  parseStagingProgress,
  validateStagingRunConsistency,
  type JsonObject,
} from "./staging-contract";

export const STAGING_RUN_FILES_SCHEMA_VERSION = 1;

/**
 * The files of a run folder that are copied, by their path inside it. A
 * version 1 run's reform validation is at the folder's top level; Microcosm's
 * version 2 writer puts it under artifacts/ (microcosm#1135). A reader that
 * knows fewer names reads an index that lists more, without those files.
 */
export const STAGING_RUN_FILE_NAMES = [
  "run_manifest.json",
  "progress.json",
  "events.ndjson",
  "calibration_progress.json",
  "reform_validation.json",
  "artifacts/staged_dataset.json",
  "artifacts/reform_validation.json",
] as const;

export type StagingRunFileName = (typeof STAGING_RUN_FILE_NAMES)[number];

export const STAGING_RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

export function isStagingRunFileName(value: unknown): value is StagingRunFileName {
  return typeof value === "string" && (STAGING_RUN_FILE_NAMES as readonly string[]).includes(value);
}

// The reform validation a structured run manifest declares, if it declares one.
function declaredReformValidation(runManifest: JsonObject | null): JsonObject | null {
  const artifacts = isStructuredStagingDocument(runManifest) ? runManifest?.artifacts : null;
  const declared =
    artifacts && typeof artifacts === "object" && !Array.isArray(artifacts)
      ? (artifacts as JsonObject).reform_validation
      : null;
  return declared && typeof declared === "object" && !Array.isArray(declared) ? (declared as JsonObject) : null;
}

/**
 * The copy that holds a run's reform validation, as the Hugging Face reader
 * reads it: the one a structured run manifest declares under artifacts/, none
 * for a structured run that declares none, and the folder's top-level file for
 * a version 1 run. `runManifest` is the parsed manifest. A declared copy counts
 * only when its digest is the declared SHA-256, as `loadCopiedStagingRun` reads
 * it.
 */
export function stagingReformValidationFileName(runManifest: JsonObject | null): StagingRunFileName | null {
  if (!isStructuredStagingDocument(runManifest)) return "reform_validation.json";
  return declaredReformValidation(runManifest) ? "artifacts/reform_validation.json" : null;
}

// The copied bytes of the run's reform validation: a structured run's declared
// copy only when it has the declared SHA-256, a version 1 run's top-level copy.
function copiedReformValidation(
  record: StagingRunRecord,
  runManifest: JsonObject | null,
  topLevel: Uint8Array | null,
  declaredCopy: Uint8Array | null,
): Uint8Array | null {
  if (!isStructuredStagingDocument(runManifest)) return topLevel;
  const declared = declaredReformValidation(runManifest);
  return declared && record.files["artifacts/reform_validation.json"]?.sha256 === declared.sha256
    ? declaredCopy
    : null;
}

export interface StagingRunFileRecord {
  path: string;
  /** The SHA-256 of the original file's bytes. */
  sha256: string;
  rawBytes: number;
  gzipBytes: number;
  /** The git blob ID Hugging Face lists for the file. */
  hfBlobId: string;
}

export const STAGING_RUN_TREE_OUTCOMES = ["ineligible", "unbuildable", "too_large", "refused"] as const;

export type StagingRunTreeOutcome = (typeof STAGING_RUN_TREE_OUTCOMES)[number];

/** The last time the job found a run's tree settled for a state of its folder. */
export interface StagingRunTreeCheck {
  folderFingerprint: string;
  /** The tree that state has; null for a run without one. */
  buildArtifactId: string | null;
  /** Why that state has no new tree, when it was final. */
  outcome?: StagingRunTreeOutcome;
  reason?: string | null;
  /** The build's source-file copies found stored with other bytes. */
  sourceFileConflicts?: string[];
}

export interface StagingRunRecord {
  runId: string;
  hfCommitSha: string;
  copiedAt: string;
  /** A digest of every file in the run's folder, by path and git blob ID. */
  folderFingerprint: string;
  summary: StagingRunSummary;
  /** Why the run's documents break the staging contract, if they do. */
  problem: string | null;
  files: Partial<Record<StagingRunFileName, StagingRunFileRecord>>;
  /** Files the run has that were too large to copy. */
  omittedFiles?: StagingRunFileName[];
  treeCheck: StagingRunTreeCheck | null;
  /** When the job found the run's folder gone from Hugging Face. */
  removedAt?: string;
}

export interface StagingRunFilesIndex {
  schemaVersion: typeof STAGING_RUN_FILES_SCHEMA_VERSION;
  country: MicrocosmCountry;
  hfRepo: string | null;
  updatedAt: string | null;
  runs: Record<string, StagingRunRecord>;
}

export interface StoredStagingRunFilesIndex {
  index: StagingRunFilesIndex;
  etag: string | null;
}

type BlobReader = Pick<CalibrationTreeBlobClient, "get">;

export function stagingRunFilesIndexPath(country: MicrocosmCountry): string {
  return `staging-run-files/${country}/index.json`;
}

export function stagingRunFilePath(country: MicrocosmCountry, runId: string, sha256: string): string {
  if (!STAGING_RUN_ID.test(runId)) throw new Error("Invalid staging run ID.");
  if (!isSha256(sha256)) throw new Error("Invalid staging run file digest.");
  return `staging-run-files/${country}/runs/${runId}/${sha256}.gz`;
}

export function emptyStagingRunFilesIndex(country: MicrocosmCountry): StagingRunFilesIndex {
  return {
    schemaVersion: STAGING_RUN_FILES_SCHEMA_VERSION,
    country,
    hfRepo: null,
    updatedAt: null,
    runs: {},
  };
}

export function parseStagingRunFilesIndex(value: unknown, country: MicrocosmCountry): StagingRunFilesIndex {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("The staging run files index must be a JSON object.");
  }
  const index = value as Partial<StagingRunFilesIndex>;
  if (index.schemaVersion !== STAGING_RUN_FILES_SCHEMA_VERSION) {
    throw new Error(`The staging run files index has schema version ${String(index.schemaVersion)}.`);
  }
  if (index.country !== country) {
    throw new Error(`The staging run files index is for country ${String(index.country)}.`);
  }
  if (!index.runs || typeof index.runs !== "object" || Array.isArray(index.runs)) {
    throw new Error("The staging run files index has no runs.");
  }
  return index as StagingRunFilesIndex;
}

/** The country's index and its ETag; an empty index while the store has none. */
export async function readStagingRunFilesIndex(options: {
  country: MicrocosmCountry;
  token?: string;
  /** Read past the cache, as a writer must. */
  consistent?: boolean;
  client?: BlobReader;
}): Promise<StoredStagingRunFilesIndex> {
  const result = await (options.client ?? { get }).get(stagingRunFilesIndexPath(options.country), {
    access: "private",
    useCache: options.consistent !== true,
    // A compressed response carries a weak ETag, which no conditional write
    // matches, so a writer asks for the index as stored.
    ...(options.consistent === true ? { headers: { "accept-encoding": "identity" } } : {}),
    ...(options.token ? { token: options.token } : {}),
  });
  if (!result) return { index: emptyStagingRunFilesIndex(options.country), etag: null };
  if (result.statusCode !== 200 || !result.stream) {
    throw new Error(`Unexpected status ${result.statusCode} reading the staging run files index.`);
  }
  return {
    index: parseStagingRunFilesIndex(await new Response(result.stream).json(), options.country),
    etag: result.blob.etag,
  };
}

/** A copied file's original bytes, checked against the digest the index records. */
export async function readStagingRunFile(options: {
  record: Pick<StagingRunFileRecord, "path" | "sha256">;
  token?: string;
  consistent?: boolean;
  client?: BlobReader;
}): Promise<Uint8Array> {
  const { path, sha256 } = options.record;
  const result = await (options.client ?? { get }).get(path, {
    access: "private",
    useCache: options.consistent !== true,
    ...(options.token ? { token: options.token } : {}),
  });
  if (!result || result.statusCode !== 200 || !result.stream) {
    throw new Error(`The staging run file ${path} could not be read.`);
  }
  const bytes = new Uint8Array(gunzipSync(await new Response(result.stream).arrayBuffer()));
  if (createHash("sha256").update(bytes).digest("hex") !== sha256) {
    throw new Error(`The staging run file ${path} does not match its digest.`);
  }
  return bytes;
}

export interface CopiedStagingRuns {
  available: true;
  source_repo: string | null;
  revision: null;
  truncated: false;
  runs: StagingRunSummary[];
  incompatible_runs: IncompatibleStagingRun[];
}

/** The country's copied runs, in the order the Hugging Face reader lists them. */
export async function loadCopiedStagingRuns(options: {
  country: MicrocosmCountry;
  token?: string;
  client?: BlobReader;
}): Promise<CopiedStagingRuns> {
  const { index } = await readStagingRunFilesIndex(options);
  const records = Object.values(index.runs).filter((record) => !record.removedAt);
  return {
    available: true,
    source_repo: index.hfRepo,
    revision: null,
    truncated: false,
    runs: records.filter((record) => record.problem === null).map((record) => record.summary).sort(sortRuns),
    incompatible_runs: records.flatMap((record) =>
      record.problem === null
        ? []
        : [{
          run_id: record.runId,
          run_manifest_path: `runs/${record.runId}/run_manifest.json`,
          detail: record.problem,
        }]
    ),
  };
}

export interface CopiedStagingRun {
  run_id: string;
  hf_repo: string | null;
  hf_commit_sha: string;
  copied_at: string;
  summary: StagingRunSummary;
  progress: JsonObject | null;
  run_manifest: JsonObject | null;
  calibration_progress: JsonObject | null;
  events: JsonObject[];
  reform_validation: JsonObject | null;
  /** The staged bundle receipt, with the digest of the stored file. */
  staged_dataset: { payload: JsonObject; sha256: string } | null;
  /** Files the run has that were too large to copy. */
  omitted_files: StagingRunFileName[];
}

// As the Hugging Face reader's stagingJsonOrNull: a file that isn't a JSON
// object reads as absent.
export function stagingJsonObjectOrNull(bytes: Uint8Array | null): JsonObject | null {
  if (!bytes) return null;
  try {
    const parsed: unknown = JSON.parse(new TextDecoder().decode(bytes));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as JsonObject) : null;
  } catch {
    return null;
  }
}

/** One run's copied documents, parsed as the Hugging Face reader parses them; null without a copy. */
export async function loadCopiedStagingRun(options: {
  country: MicrocosmCountry;
  runId: string;
  token?: string;
  client?: BlobReader;
}): Promise<CopiedStagingRun | null> {
  const { index } = await readStagingRunFilesIndex(options);
  const record = Object.hasOwn(index.runs, options.runId) ? index.runs[options.runId] : null;
  if (!record || record.removedAt) return null;
  const read = async (name: StagingRunFileName): Promise<Uint8Array | null> => {
    const file = record.files[name];
    return file ? readStagingRunFile({ record: file, token: options.token, client: options.client }) : null;
  };
  const [manifest, progress, events, calibration, topLevelReformValidation, stagedDataset, declaredReformValidationCopy] =
    await Promise.all(STAGING_RUN_FILE_NAMES.map(read));
  const manifestDocument = stagingJsonObjectOrNull(manifest);
  const progressDocument = stagingJsonObjectOrNull(progress);
  const calibrationDocument = stagingJsonObjectOrNull(calibration);
  const receipt = stagingJsonObjectOrNull(stagedDataset);
  const runManifest = manifestDocument && parseStagingManifest(manifestDocument);
  const parsedProgress = progressDocument && parseStagingProgress(progressDocument);
  const calibrationProgress = calibrationDocument && parseStagingCalibrationProgress(calibrationDocument);
  const parsedEvents = parseStagingEvents(events && new TextDecoder().decode(events));
  validateStagingRunConsistency(record.runId, {
    progress: parsedProgress,
    runManifest,
    calibrationProgress,
    events: parsedEvents,
  });
  return {
    run_id: record.runId,
    hf_repo: index.hfRepo,
    hf_commit_sha: record.hfCommitSha,
    copied_at: record.copiedAt,
    summary: record.summary,
    progress: parsedProgress,
    run_manifest: runManifest,
    calibration_progress: calibrationProgress,
    events: parsedEvents,
    reform_validation: stagingJsonObjectOrNull(
      copiedReformValidation(record, runManifest, topLevelReformValidation, declaredReformValidationCopy),
    ),
    staged_dataset: receipt && {
      payload: receipt,
      sha256: record.files["artifacts/staged_dataset.json"]!.sha256,
    },
    omitted_files: record.omittedFiles ?? [],
  };
}
