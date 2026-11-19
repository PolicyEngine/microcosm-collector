// Replaces the collector's copy of a country's releases.json in the tree store
// (calibration-tree-releases-record.ts). The collector runs it as its own Bun
// process, with the store's read-write token in
// CALIBRATION_TREE_BLOB_READ_WRITE_TOKEN, in two steps around its read of
// Hugging Face:
//
//   bun run src/calibration-tree-releases-record-copy.ts read COUNTRY
//   bun run src/calibration-tree-releases-record-copy.ts write COPY.json
//
// `read` prints the stored object's version as one JSON line:
//
//   {"country": "us", "etag": "\"...\"", "hfCommitSha": "...", "hasRecord": true}
//
// All three are null without a stored object. `hfCommitSha` and `hasRecord`
// are null when the object breaks the document's rules, so it can be replaced.
//
// `write` takes COPY.json, holding:
//
//   {"country": "us", "hfRepo": "<owner>/<name>", "hfCommitSha": "...",
//    "expectedEtag": "\"...\"", "recordFile": "<path of releases.json>"}
//
// `expectedEtag` is the ETag `read` printed, or null when it found no object.
// `recordFile` is null when the repo has no releases.json at that commit. The
// write replaces only that version of the object, or creates the object only
// if there still is none, and prints one JSON line:
//
//   {"country": "us", "hfCommitSha": "...", "hasRecord": true,
//    "sha256": "...", "etag": "\"...\""}
//
// The collector reads the version before it reads Hugging Face's head. A write
// that succeeds therefore saw the previous write's version before it read its
// head, so its head is at least as new, and an older execution cannot replace
// a newer copy.
//
// The exit codes other than 0:
//
//   1  the request could not be followed: no token, a malformed request or a
//      record file that cannot be read;
//   2  the package's rules refuse the record; nothing was written;
//   3  anything else, including any error from the store; running again may
//      succeed;
//   4  another execution changed the object after `read`; nothing was written,
//      and the collector starts again from `read`.
import { BlobPreconditionFailedError, get, put } from "@vercel/blob";
import { readFileSync } from "node:fs";

import type { CalibrationTreeBlobClient } from "./calibration-tree-blob";
import { CALIBRATION_TREE_PUBLICATION_TOKEN_VARIABLE, isWeakEtag } from "./calibration-tree-publication";
import {
  isHfCommitSha,
  isHfRepo,
  parseStoredReleasesRecord,
  storedReleasesRecordDocument,
  storedReleasesRecordPath,
} from "./calibration-tree-releases-record";
import { isCountry, type MicrocosmCountry } from "./countries";
import { ReleasesRecordError } from "./release-record";

export const RELEASES_RECORD_COPY_EXIT_REQUEST_ERROR = 1;
export const RELEASES_RECORD_COPY_EXIT_REFUSED = 2;
export const RELEASES_RECORD_COPY_EXIT_STORE_ERROR = 3;
export const RELEASES_RECORD_COPY_EXIT_CONFLICT = 4;

// As the manifest is written: the copy is replaced, so readers see a change
// within a minute.
const COPY_CACHE_SECONDS = 60;
const USAGE =
  "Usage: bun run src/calibration-tree-releases-record-copy.ts read COUNTRY | write COPY.json";
const REQUEST_KEYS = ["country", "expectedEtag", "hfCommitSha", "hfRepo", "recordFile"];

/** The request could not be followed; nothing was read or written. */
export class ReleasesRecordCopyRequestError extends Error {}

/** Another execution changed the stored object first; nothing was written. */
export class ReleasesRecordCopyConflictError extends Error {}

export interface ReleasesRecordCopyRequest {
  country: MicrocosmCountry;
  hfRepo: string;
  hfCommitSha: string;
  /** The stored object's ETag as read, or null when there was none. */
  expectedEtag: string | null;
  /** The path of the repo's releases.json, or null when it has none. */
  recordFile: string | null;
}

export interface ReleasesRecordCopyVersion {
  country: MicrocosmCountry;
  etag: string | null;
  hfCommitSha: string | null;
  hasRecord: boolean | null;
}

export interface WrittenReleasesRecordCopy {
  country: MicrocosmCountry;
  hfCommitSha: string;
  hasRecord: boolean;
  sha256: string | null;
  etag: string;
}

function parseCountry(value: unknown): MicrocosmCountry {
  if (typeof value !== "string" || !isCountry(value)) {
    throw new ReleasesRecordCopyRequestError(`country ${String(value)} is not a registered country.`);
  }
  return value;
}

/** Checks a copy request read from JSON. */
export function parseReleasesRecordCopyRequest(value: unknown): ReleasesRecordCopyRequest {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ReleasesRecordCopyRequestError("The request must be a JSON object.");
  }
  const request = value as Record<string, unknown>;
  const present = Object.keys(request).sort();
  if (present.length !== REQUEST_KEYS.length || present.some((key, index) => key !== REQUEST_KEYS[index])) {
    throw new ReleasesRecordCopyRequestError(`The request must have exactly ${REQUEST_KEYS.join(", ")}.`);
  }
  const { hfRepo, hfCommitSha, expectedEtag, recordFile } = request;
  const country = parseCountry(request.country);
  if (!isHfRepo(hfRepo)) throw new ReleasesRecordCopyRequestError("hfRepo must be <owner>/<name>.");
  if (!isHfCommitSha(hfCommitSha)) throw new ReleasesRecordCopyRequestError("hfCommitSha must be a commit.");
  if (expectedEtag !== null && (typeof expectedEtag !== "string" || !expectedEtag || isWeakEtag(expectedEtag))) {
    throw new ReleasesRecordCopyRequestError("expectedEtag must be a strong ETag or null.");
  }
  if (recordFile !== null && (typeof recordFile !== "string" || !recordFile)) {
    throw new ReleasesRecordCopyRequestError("recordFile must be a path or null.");
  }
  return { country, hfRepo, hfCommitSha, expectedEtag, recordFile };
}

const DEFAULT_CLIENT: CalibrationTreeBlobClient = { get, put };

/**
 * The stored object's version, read past the cache and uncompressed: Vercel
 * Blob gives a compressed response the weak form of the ETag, which no
 * conditional write matches.
 */
export async function readReleasesRecordCopyVersion(options: {
  country: MicrocosmCountry;
  token: string;
  client?: CalibrationTreeBlobClient;
}): Promise<ReleasesRecordCopyVersion> {
  const { country, token, client = DEFAULT_CLIENT } = options;
  const path = storedReleasesRecordPath(country);
  const result = await client.get(path, {
    access: "private",
    useCache: false,
    headers: { "accept-encoding": "identity" },
    token,
  });
  if (!result) return { country, etag: null, hfCommitSha: null, hasRecord: null };
  if (result.statusCode !== 200 || !result.stream) {
    throw new Error(`Unexpected status ${result.statusCode} reading ${path}.`);
  }
  const etag = result.blob.etag;
  if (!etag) throw new Error(`${path} was read without an ETag, so it cannot be replaced safely.`);
  if (isWeakEtag(etag)) {
    throw new Error(`${path} was read with a weak ETag, which no conditional write matches.`);
  }
  const text = await new Response(result.stream).text();
  try {
    const stored = parseStoredReleasesRecord(text, country);
    return { country, etag, hfCommitSha: stored.hfCommitSha, hasRecord: stored.state === "record" };
  } catch {
    // The object can still be replaced under its ETag.
    return { country, etag, hfCommitSha: null, hasRecord: null };
  }
}

function isPreconditionFailure(error: unknown): boolean {
  return error instanceof BlobPreconditionFailedError ||
    (error instanceof Error && /precondition failed.*etag mismatch/i.test(error.message));
}

function readRecordFile(recordFile: string | null): Uint8Array | null {
  if (recordFile === null) return null;
  try {
    return new Uint8Array(readFileSync(recordFile));
  } catch (error) {
    throw new ReleasesRecordCopyRequestError(
      `The record ${recordFile} could not be read: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
}

/**
 * Replaces the version `request.expectedEtag` names, or creates the object if
 * it names none. Throws ReleasesRecordCopyConflictError when another execution
 * changed the object first, and ReleasesRecordError for a refused record.
 */
export async function writeReleasesRecordCopy(options: {
  request: ReleasesRecordCopyRequest;
  token: string;
  client?: CalibrationTreeBlobClient;
  now?: () => Date;
}): Promise<WrittenReleasesRecordCopy> {
  const { request, token, client = DEFAULT_CLIENT, now = () => new Date() } = options;
  const { country, hfRepo, hfCommitSha, expectedEtag } = request;
  const recordBytes = readRecordFile(request.recordFile);
  const documentText = storedReleasesRecordDocument({
    country,
    hfRepo,
    hfCommitSha,
    copiedAt: now().toISOString(),
    recordBytes,
  });
  const path = storedReleasesRecordPath(country);
  const conflict = () =>
    new ReleasesRecordCopyConflictError(`Another execution changed ${path} after it was read.`);
  let etag: string;
  try {
    ({ etag } = await client.put(path, documentText, {
      access: "private",
      token,
      addRandomSuffix: false,
      allowOverwrite: expectedEtag !== null,
      ...(expectedEtag !== null ? { ifMatch: expectedEtag } : {}),
      contentType: "application/json; charset=utf-8",
      cacheControlMaxAge: COPY_CACHE_SECONDS,
    }));
  } catch (error) {
    if (isPreconditionFailure(error)) throw conflict();
    // A create is refused when the object exists, by an error without a code
    // of its own, so the store is asked.
    if (expectedEtag === null) {
      const current = await readReleasesRecordCopyVersion({ country, token, client });
      if (current.etag !== null) throw conflict();
    }
    throw error;
  }
  // Another execution may already have replaced this write, which is fine; the
  // object must only no longer be the version this write replaced.
  const verified = await readReleasesRecordCopyVersion({ country, token, client });
  if (
    verified.etag === null ||
    verified.etag === expectedEtag ||
    (verified.etag === etag && verified.hfCommitSha !== hfCommitSha)
  ) {
    throw new Error(`${path} failed verification after it was written.`);
  }
  const stored = parseStoredReleasesRecord(documentText, country);
  return {
    country,
    hfCommitSha,
    hasRecord: stored.state === "record",
    sha256: stored.state === "record" ? stored.sha256 : null,
    etag,
  };
}

/** The exit code for an error that stopped the command. */
export function releasesRecordCopyExitCode(error: unknown): number {
  if (error instanceof ReleasesRecordCopyRequestError) return RELEASES_RECORD_COPY_EXIT_REQUEST_ERROR;
  if (error instanceof ReleasesRecordError) return RELEASES_RECORD_COPY_EXIT_REFUSED;
  if (error instanceof ReleasesRecordCopyConflictError) return RELEASES_RECORD_COPY_EXIT_CONFLICT;
  return RELEASES_RECORD_COPY_EXIT_STORE_ERROR;
}

function requestFromFile(file: string): ReleasesRecordCopyRequest {
  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(file, "utf8"));
  } catch (error) {
    throw new ReleasesRecordCopyRequestError(
      `The request ${file} could not be read as JSON: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  return parseReleasesRecordCopyRequest(parsed);
}

/** The command line: returns the process exit code. */
export async function releasesRecordCopyMain(
  argv: string[],
  environment: Record<string, string | undefined> = process.env,
  client?: CalibrationTreeBlobClient,
  now?: () => Date,
): Promise<number> {
  try {
    const [action, argument] = argv;
    if (argv.length !== 2 || (action !== "read" && action !== "write") || !argument) {
      throw new ReleasesRecordCopyRequestError(USAGE);
    }
    const token = environment[CALIBRATION_TREE_PUBLICATION_TOKEN_VARIABLE]?.trim();
    if (!token) {
      throw new ReleasesRecordCopyRequestError(`${CALIBRATION_TREE_PUBLICATION_TOKEN_VARIABLE} is required.`);
    }
    const answer = action === "read"
      ? await readReleasesRecordCopyVersion({ country: parseCountry(argument), token, client })
      : await writeReleasesRecordCopy({ request: requestFromFile(argument), token, client, now });
    console.log(JSON.stringify(answer));
    return 0;
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    return releasesRecordCopyExitCode(error);
  }
}

if (import.meta.main) {
  process.exitCode = await releasesRecordCopyMain(Bun.argv.slice(2));
}
