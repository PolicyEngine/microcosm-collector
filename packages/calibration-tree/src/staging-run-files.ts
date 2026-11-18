// The stored copies of staging run files. The collector's webhook-queued job
// copies the files the dashboard reads for every run in a country's Hugging
// Face staging repository to private blobs:
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
  parseStagingCalibrationProgress,
  parseStagingEvents,
  parseStagingManifest,
  parseStagingProgress,
  validateStagingRunConsistency,
  type JsonObject,
} from "./staging-contract";

export const STAGING_RUN_FILES_SCHEMA_VERSION = 1;

/** The files of a run folder that are copied, by their path inside it. */
export const STAGING_RUN_FILE_NAMES = [
  "run_manifest.json",
  "progress.json",
  "events.ndjson",
  "calibration_progress.json",
  "reform_validation.json",
  "artifacts/staged_dataset.json",
] as const;

export type StagingRunFileName = (typeof STAGING_RUN_FILE_NAMES)[number];

export const STAGING_RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

export function isStagingRunFileName(value: unknown): value is StagingRunFileName {
  return typeof value === "string" && (STAGING_RUN_FILE_NAMES as readonly string[]).includes(value);
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

/** The last time the job found a run's tree settled for a state of its folder. */
export interface StagingRunTreeCheck {
  folderFingerprint: string;
  /** The tree that state has; null for a run without diagnostics. */
  buildArtifactId: string | null;
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
  treeCheck: StagingRunTreeCheck | null;
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
  const records = Object.values(index.runs);
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
  /** The staged bundle receipt, with the digest its run manifest declares. */
  staged_dataset: { payload: JsonObject; sha256: string } | null;
}

function jsonObject(bytes: Uint8Array, name: string): JsonObject {
  let parsed: unknown;
  try {
    parsed = JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    throw new Error(`${name} is not valid JSON.`);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`${name} must be a JSON object.`);
  }
  return parsed as JsonObject;
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
  if (!record) return null;
  const read = async (name: StagingRunFileName): Promise<Uint8Array | null> => {
    const file = record.files[name];
    return file ? readStagingRunFile({ record: file, token: options.token, client: options.client }) : null;
  };
  const [manifest, progress, events, calibration, reformValidation, stagedDataset] = await Promise.all(
    STAGING_RUN_FILE_NAMES.map(read),
  );
  const runManifest = manifest && parseStagingManifest(jsonObject(manifest, "run_manifest.json"));
  const parsedProgress = progress && parseStagingProgress(jsonObject(progress, "progress.json"));
  const calibrationProgress = calibration &&
    parseStagingCalibrationProgress(jsonObject(calibration, "calibration_progress.json"));
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
    reform_validation: reformValidation && jsonObject(reformValidation, "reform_validation.json"),
    staged_dataset: stagedDataset && {
      payload: jsonObject(stagedDataset, "artifacts/staged_dataset.json"),
      sha256: record.files["artifacts/staged_dataset.json"]!.sha256,
    },
  };
}
