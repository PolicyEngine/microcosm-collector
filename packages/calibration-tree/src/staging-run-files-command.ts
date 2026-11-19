// The collector's access to the stored copies of staging run files. The
// webhook-queued job runs it as its own Bun process, under its country's and
// kind's lock, with the store's token in CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN:
//
//   bun run src/staging-run-files-command.ts read <country>
//   bun run src/staging-run-files-command.ts publish REQUEST.json
//   bun run src/staging-run-files-command.ts tree-sources <country> <build ID>
//
// `read` prints the country's index, an empty one while the store has none.
// `publish` stores the request's files and then replaces the index, only if it
// is still the version read, and prints how many files each run uploaded and
// kept. A file whose digest the run's record already holds is not stored again,
// and earlier versions stay. `tree-sources` prints the source files a stored
// tree records, so the job can tell whether a run's files have changed since.
// Each prints one JSON line and exits 0. The other exit codes:
//
//   1  the request could not be followed: no token, unknown arguments or a
//      malformed request;
//   3  anything else, including an index another writer changed and any error
//      from the store; running again may succeed.
import { get, put } from "@vercel/blob";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { gzipSync } from "node:zlib";

import { isSha256 } from "./calibration-tree-artifact";
import {
  calibrationTreeBlobText,
  getCalibrationTreeBlob,
  type CalibrationTreeBlobClient,
} from "./calibration-tree-blob";
import {
  CALIBRATION_TREE_PUBLICATION_TOKEN_VARIABLE,
  CALIBRATION_TREE_WEAK_ETAG_MESSAGE,
  isWeakEtag,
} from "./calibration-tree-publication";
import { isCountry, type MicrocosmCountry } from "./countries";
import { summaryFromManifest, summaryFromProgress, type StagingRunSummary } from "./staging-artifact";
import {
  isStructuredStagingDocument,
  parseStagingManifest,
  parseStagingProgress,
  parseStagingRunIndex,
  validateStagingRunConsistency,
} from "./staging-contract";
import {
  isStagingRunFileName,
  readStagingRunFile,
  readStagingRunFilesIndex,
  STAGING_RUN_ID,
  STAGING_RUN_TREE_OUTCOMES,
  stagingJsonObjectOrNull,
  stagingRunFilePath,
  stagingRunFilesIndexPath,
  type StagingRunFileName,
  type StagingRunFileRecord,
  type StagingRunFilesIndex,
  type StagingRunTreeCheck,
  type StagingRunTreeOutcome,
} from "./staging-run-files";

export const STAGING_RUN_FILES_EXIT_REQUEST_ERROR = 1;
export const STAGING_RUN_FILES_EXIT_STORE_ERROR = 3;

const IMMUTABLE_CACHE_SECONDS = 31_536_000;
const INDEX_CACHE_SECONDS = 60;
const HF_COMMIT = /^[0-9a-f]{40}$/;

/** The request could not be followed; nothing was stored. */
export class StagingRunFilesRequestError extends Error {}

export interface StagingRunFilesPublicationFile {
  name: StagingRunFileName;
  /** The downloaded file on disk. */
  file: string;
  hfBlobId: string;
}

export interface StagingRunFilesPublicationRun {
  runId: string;
  folderFingerprint: string;
  /** The run's row in a schema-1 runs.json, if it has one. */
  indexRow: unknown;
  files: StagingRunFilesPublicationFile[];
  omittedFiles: StagingRunFileName[];
}

export interface StagingRunFilesTreeCheck {
  runId: string;
  folderFingerprint: string;
  buildArtifactId: string | null;
  outcome: StagingRunTreeOutcome | null;
  reason: string | null;
  sourceFileConflicts: string[];
}

export interface StagingRunFilesPublication {
  country: MicrocosmCountry;
  hfRepo: string;
  hfCommitSha: string;
  runs: StagingRunFilesPublicationRun[];
  treeChecks: StagingRunFilesTreeCheck[];
  /** Runs whose folders the job no longer finds on Hugging Face. */
  removedRunIds: string[];
}

type JsonRecord = Record<string, unknown>;

function requestObject(value: unknown, label: string): JsonRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new StagingRunFilesRequestError(`${label} must be a JSON object.`);
  }
  return value as JsonRecord;
}

function requestString(value: JsonRecord, field: string, label: string): string {
  const text = value[field];
  if (typeof text !== "string" || !text) {
    throw new StagingRunFilesRequestError(`${label}.${field} must be a non-empty string.`);
  }
  return text;
}

function requestList(value: JsonRecord, field: string): unknown[] {
  const list = value[field];
  if (!Array.isArray(list)) throw new StagingRunFilesRequestError(`${field} must be a list.`);
  return list;
}

function runIdentity(value: JsonRecord, label: string): { runId: string; folderFingerprint: string } {
  const runId = requestString(value, "runId", label);
  const folderFingerprint = requestString(value, "folderFingerprint", label);
  if (!STAGING_RUN_ID.test(runId)) throw new StagingRunFilesRequestError(`${label}.runId is not a run ID.`);
  if (!isSha256(folderFingerprint)) {
    throw new StagingRunFilesRequestError(`${label}.folderFingerprint is not a SHA-256.`);
  }
  return { runId, folderFingerprint };
}

function publicationFile(value: unknown): StagingRunFilesPublicationFile {
  const file = requestObject(value, "A run file");
  const name = file.name;
  if (!isStagingRunFileName(name)) {
    throw new StagingRunFilesRequestError(`${String(name)} is not a copied run file.`);
  }
  return {
    name,
    file: requestString(file, "file", "A run file"),
    hfBlobId: requestString(file, "hfBlobId", "A run file"),
  };
}

function publicationRun(value: unknown): StagingRunFilesPublicationRun {
  const run = requestObject(value, "A run");
  const files = requestList(run, "files").map(publicationFile);
  if (new Set(files.map((file) => file.name)).size !== files.length) {
    throw new StagingRunFilesRequestError("A run names a file twice.");
  }
  const omittedFiles = (run.omittedFiles ?? []) as unknown;
  if (!Array.isArray(omittedFiles) || !omittedFiles.every(isStagingRunFileName)) {
    throw new StagingRunFilesRequestError("A run's omittedFiles must name copied run files.");
  }
  return { ...runIdentity(run, "A run"), indexRow: run.indexRow ?? null, files, omittedFiles };
}

function publicationTreeCheck(value: unknown): StagingRunFilesTreeCheck {
  const check = requestObject(value, "A tree check");
  const buildArtifactId = check.buildArtifactId ?? null;
  if (buildArtifactId !== null && (typeof buildArtifactId !== "string" || !isSha256(buildArtifactId))) {
    throw new StagingRunFilesRequestError("A tree check's buildArtifactId is not a build ID.");
  }
  const outcome = check.outcome ?? null;
  if (outcome !== null && !(STAGING_RUN_TREE_OUTCOMES as readonly unknown[]).includes(outcome)) {
    throw new StagingRunFilesRequestError(`${String(outcome)} is not a final tree outcome.`);
  }
  const reason = check.reason ?? null;
  if (reason !== null && typeof reason !== "string") {
    throw new StagingRunFilesRequestError("A tree check's reason must be a string.");
  }
  const sourceFileConflicts = (check.sourceFileConflicts ?? []) as unknown;
  if (!Array.isArray(sourceFileConflicts) || !sourceFileConflicts.every((file) => typeof file === "string")) {
    throw new StagingRunFilesRequestError("A tree check's sourceFileConflicts must be a list of names.");
  }
  return {
    ...runIdentity(check, "A tree check"),
    buildArtifactId,
    outcome: outcome as StagingRunTreeOutcome | null,
    reason,
    sourceFileConflicts,
  };
}

/** Checks a publication request read from JSON. */
export function parseStagingRunFilesPublication(value: unknown): StagingRunFilesPublication {
  const request = requestObject(value, "The request");
  const country = request.country;
  if (typeof country !== "string" || !isCountry(country)) {
    throw new StagingRunFilesRequestError(`${String(country)} is not a registered country.`);
  }
  const hfCommitSha = requestString(request, "hfCommitSha", "The request");
  if (!HF_COMMIT.test(hfCommitSha)) throw new StagingRunFilesRequestError("hfCommitSha is not a commit.");
  return {
    country,
    hfRepo: requestString(request, "hfRepo", "The request"),
    hfCommitSha,
    runs: requestList(request, "runs").map(publicationRun),
    treeChecks: requestList(request, "treeChecks").map(publicationTreeCheck),
    removedRunIds: (request.removedRunIds === undefined ? [] : requestList(request, "removedRunIds")).map((runId) => {
      if (typeof runId !== "string" || !STAGING_RUN_ID.test(runId)) {
        throw new StagingRunFilesRequestError("removedRunIds must hold run IDs.");
      }
      return runId;
    }),
  };
}

function parsedDocument(bytes: Uint8Array, name: string): unknown {
  try {
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    throw new Error(`${name} is not valid JSON.`);
  }
}

// The run's list entry: a structured run's manifest, else its runs.json row,
// else its progress, else a schema-1 manifest, which the Hugging Face reader does
// not fall back to. A progress file that isn't a JSON object is absent. The job
// passes a row only when it has read runs.json.
function describedRun(
  runId: string,
  files: ReadonlyMap<StagingRunFileName, Uint8Array>,
  indexRow: unknown,
): { summary: StagingRunSummary; problem: string | null } {
  try {
    const manifestBytes = files.get("run_manifest.json");
    const manifest = manifestBytes
      ? parseStagingManifest(parsedDocument(manifestBytes, "run_manifest.json"))
      : null;
    if (manifest) {
      if (manifest.run_id !== runId) {
        throw new Error(`run_manifest.json names run ${String(manifest.run_id)}, not ${runId}.`);
      }
      validateStagingRunConsistency(runId, { runManifest: manifest });
      if (isStructuredStagingDocument(manifest)) {
        return { summary: summaryFromManifest(runId, manifest), problem: null };
      }
    }
    if (indexRow) {
      return { summary: parseStagingRunIndex({ schema_version: 1, runs: [indexRow] })[0], problem: null };
    }
    const progressDocument = stagingJsonObjectOrNull(files.get("progress.json") ?? null);
    const progress = progressDocument && parseStagingProgress(progressDocument);
    validateStagingRunConsistency(runId, { progress });
    let summary: StagingRunSummary;
    if (progress) summary = summaryFromProgress(runId, progress);
    else if (manifest) summary = summaryFromManifest(runId, manifest);
    else summary = summaryFromProgress(runId, null);
    return { summary, problem: null };
  } catch (error) {
    return {
      summary: summaryFromProgress(runId, null),
      problem: error instanceof Error ? error.message : String(error),
    };
  }
}

export interface StagingRunFilesPublicationResult {
  runs: Record<string, { uploaded: number; kept: number }>;
}

const DEFAULT_CLIENT: CalibrationTreeBlobClient = { get, put };

/** Stores the request's run files, then replaces the country's index. */
export async function publishStagingRunFiles(options: {
  request: StagingRunFilesPublication;
  token: string;
  client?: CalibrationTreeBlobClient;
  now?: () => Date;
}): Promise<StagingRunFilesPublicationResult> {
  const { request, token, client = DEFAULT_CLIENT, now = () => new Date() } = options;
  const { country } = request;
  const stored = await readStagingRunFilesIndex({ country, token, consistent: true, client });
  if (stored.etag !== null && !stored.etag) {
    throw new Error("The staging run files index was read without an ETag, so it cannot be replaced safely.");
  }
  if (stored.etag !== null && isWeakEtag(stored.etag)) throw new Error(CALIBRATION_TREE_WEAK_ETAG_MESSAGE);
  const copiedAt = now().toISOString();
  const index: StagingRunFilesIndex = {
    ...stored.index,
    hfRepo: request.hfRepo,
    updatedAt: copiedAt,
    runs: { ...stored.index.runs },
  };
  const result: StagingRunFilesPublicationResult = { runs: {} };
  for (const run of request.runs) {
    const previous = Object.hasOwn(index.runs, run.runId) ? index.runs[run.runId] : null;
    const contents = new Map<StagingRunFileName, Uint8Array>();
    const files: Partial<Record<StagingRunFileName, StagingRunFileRecord>> = {};
    let uploaded = 0;
    let kept = 0;
    for (const file of run.files) {
      const bytes = new Uint8Array(readFileSync(file.file));
      contents.set(file.name, bytes);
      const sha256 = createHash("sha256").update(bytes).digest("hex");
      const existing = previous?.files[file.name];
      if (existing?.sha256 === sha256) {
        files[file.name] = { ...existing, hfBlobId: file.hfBlobId };
        kept += 1;
        continue;
      }
      const path = stagingRunFilePath(country, run.runId, sha256);
      const compressed = gzipSync(bytes);
      // The path holds the digest, so writing it again stores the same bytes.
      await client.put(path, Buffer.from(compressed), {
        access: "private",
        token,
        addRandomSuffix: false,
        allowOverwrite: true,
        contentType: "application/gzip",
        cacheControlMaxAge: IMMUTABLE_CACHE_SECONDS,
      });
      await readStagingRunFile({ record: { path, sha256 }, token, consistent: true, client });
      files[file.name] = {
        path,
        sha256,
        rawBytes: bytes.byteLength,
        gzipBytes: compressed.byteLength,
        hfBlobId: file.hfBlobId,
      };
      uploaded += 1;
    }
    index.runs[run.runId] = {
      runId: run.runId,
      hfCommitSha: request.hfCommitSha,
      copiedAt,
      folderFingerprint: run.folderFingerprint,
      ...describedRun(run.runId, contents, run.indexRow),
      files,
      ...(run.omittedFiles.length ? { omittedFiles: run.omittedFiles } : {}),
      treeCheck: previous?.treeCheck ?? null,
    };
    result.runs[run.runId] = { uploaded, kept };
  }
  for (const check of request.treeChecks) {
    if (!Object.hasOwn(index.runs, check.runId)) continue;
    const treeCheck: StagingRunTreeCheck = {
      folderFingerprint: check.folderFingerprint,
      buildArtifactId: check.buildArtifactId,
      ...(check.outcome ? { outcome: check.outcome, reason: check.reason } : {}),
      ...(check.sourceFileConflicts.length ? { sourceFileConflicts: check.sourceFileConflicts } : {}),
    };
    index.runs[check.runId] = { ...index.runs[check.runId], treeCheck };
  }
  for (const runId of request.removedRunIds) {
    if (Object.hasOwn(index.runs, runId)) index.runs[runId] = { ...index.runs[runId], removedAt: copiedAt };
  }
  await client.put(stagingRunFilesIndexPath(country), JSON.stringify(index), {
    access: "private",
    token,
    addRandomSuffix: false,
    allowOverwrite: stored.etag !== null,
    ifMatch: stored.etag ?? undefined,
    contentType: "application/json; charset=utf-8",
    cacheControlMaxAge: INDEX_CACHE_SECONDS,
  });
  return result;
}

/** The source files a stored tree's index records. */
export async function readStoredTreeSources(options: {
  country: MicrocosmCountry;
  buildArtifactId: string;
  token: string;
  client?: CalibrationTreeBlobClient;
}): Promise<JsonRecord> {
  const blob = await getCalibrationTreeBlob({
    country: options.country,
    buildArtifactId: options.buildArtifactId,
    part: "index",
    token: options.token,
    consistent: true,
    client: options.client ?? DEFAULT_CLIENT,
  });
  if (!blob) throw new Error(`The store holds no index for tree ${options.buildArtifactId}.`);
  const index = JSON.parse(await calibrationTreeBlobText(blob.stream)) as { build?: { sourceArtifacts?: unknown } };
  const sources = index.build?.sourceArtifacts;
  if (!sources || typeof sources !== "object" || Array.isArray(sources)) {
    throw new Error(`Tree ${options.buildArtifactId} records no source files.`);
  }
  return sources as JsonRecord;
}

function countryArgument(value: string | undefined): MicrocosmCountry {
  if (value === undefined || !isCountry(value)) {
    throw new StagingRunFilesRequestError(`${String(value)} is not a registered country.`);
  }
  return value;
}

function publicationFromFile(path: string): StagingRunFilesPublication {
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new StagingRunFilesRequestError(
      `The request ${path} could not be read as JSON: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  return parseStagingRunFilesPublication(parsed);
}

const USAGE =
  "Usage: bun run src/staging-run-files-command.ts read <country> | publish REQUEST.json | tree-sources <country> <build ID>";

async function commandAnswer(argv: string[], token: string, client: CalibrationTreeBlobClient): Promise<unknown> {
  const [action, ...operands] = argv;
  if (action === "read" && operands.length === 1) {
    const { index } = await readStagingRunFilesIndex({
      country: countryArgument(operands[0]),
      token,
      consistent: true,
      client,
    });
    return index;
  }
  if (action === "publish" && operands.length === 1) {
    return publishStagingRunFiles({ request: publicationFromFile(operands[0]), token, client });
  }
  if (action === "tree-sources" && operands.length === 2) {
    const country = countryArgument(operands[0]);
    if (!isSha256(operands[1])) throw new StagingRunFilesRequestError("The build ID is not a SHA-256.");
    return readStoredTreeSources({ country, buildArtifactId: operands[1], token, client });
  }
  throw new StagingRunFilesRequestError(USAGE);
}

/** Runs the command with its arguments and returns the process exit code. */
export async function stagingRunFilesCommandMain(
  argv: string[],
  environment: Record<string, string | undefined> = process.env,
  client: CalibrationTreeBlobClient = DEFAULT_CLIENT,
): Promise<number> {
  try {
    const token = environment[CALIBRATION_TREE_PUBLICATION_TOKEN_VARIABLE]?.trim();
    if (!token) {
      throw new StagingRunFilesRequestError(`${CALIBRATION_TREE_PUBLICATION_TOKEN_VARIABLE} is required.`);
    }
    console.log(JSON.stringify(await commandAnswer(argv, token, client)));
    return 0;
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    return error instanceof StagingRunFilesRequestError
      ? STAGING_RUN_FILES_EXIT_REQUEST_ERROR
      : STAGING_RUN_FILES_EXIT_STORE_ERROR;
  }
}

if (import.meta.main) {
  process.exitCode = await stagingRunFilesCommandMain(Bun.argv.slice(2));
}
